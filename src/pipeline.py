"""Ingest pipeline: upserts CollectedItems into the DB and enriches with Unpaywall."""
from __future__ import annotations

import hashlib
import logging
import re
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urldefrag

from sqlalchemy import select, update, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.collectors.base import CollectedItem, normalize_title
from src.storage.db import AsyncSessionLocal
from src.storage.models import CrawledItem, Surface

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# P2-C: Canonical URL normalisation
# ---------------------------------------------------------------------------

_STRIP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "source", "via", "from", "referrer", "campaign",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
}


def _normalize_url(url: str) -> str:
    """Strip tracking params, lowercase scheme/host, drop fragment."""
    try:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
        filtered = {k: v for k, v in params.items() if k.lower() not in _STRIP_PARAMS}
        new_query = urllib.parse.urlencode(filtered, doseq=True)
        normalized = parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            query=new_query,
            fragment="",
        )
        return urllib.parse.urlunparse(normalized).rstrip("/") or url
    except Exception:
        return url


# ---------------------------------------------------------------------------
# P2-A: Language detection (fallback heuristic if langdetect not installed)
# ---------------------------------------------------------------------------

def _detect_lang(text: str | None) -> str:
    """Detect language of text; returns ISO 639-1 code, defaults to 'en'."""
    if not text or len(text) < 50:
        return "en"
    try:
        from langdetect import detect  # type: ignore
        return detect(text) or "en"
    except Exception:
        return "en"


# ---------------------------------------------------------------------------
# P2-E: Content quality gate
# ---------------------------------------------------------------------------

def _passes_quality_gate(content_body: str | None) -> bool:
    """Return True if content_body is worth storing; None/too-short = False."""
    if not content_body:
        return True  # no content body — metadata-only items are still indexed
    text = content_body.strip()
    if len(text) < 150:
        return False
    return True


# ---------------------------------------------------------------------------
# P2-F: Staleness flag
# ---------------------------------------------------------------------------

def _compute_is_stale(published_at: datetime | None) -> bool | None:
    """Return True if published_at is older than 5 years, else False, else None."""
    if not published_at:
        return None
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=5 * 365)
    if published_at.tzinfo is None:
        return None
    return published_at < cutoff


# ---------------------------------------------------------------------------
# P3-C: Evidence level inference
# ---------------------------------------------------------------------------

_EVIDENCE_KEYWORDS: dict[str, list[str]] = {
    "systematic_review": ["systematic review", "meta-analysis", "cochrane"],
    "rct": ["randomized controlled", "randomised controlled", "rct", "double-blind"],
    "cohort": ["cohort study", "longitudinal study", "prospective study"],
    "case_study": ["case study", "case report", "case series"],
    "expert_opinion": ["expert opinion", "editorial", "commentary", "perspective"],
    "guideline": ["clinical guideline", "practice guideline", "dsm-5", "icd-"],
}


def _infer_evidence_level(
    title: str,
    description: str | None,
    source_type: str | None,
) -> str | None:
    if source_type in ("forum", "reddit", "social"):
        return "anecdotal"
    if source_type == "blog":
        return "blog"
    text = ((title or "") + " " + (description or "")).lower()
    for level, keywords in _EVIDENCE_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return level
    if source_type == "peer_reviewed":
        return "peer_reviewed"
    return None


# ---------------------------------------------------------------------------
# P3-B: Near-duplicate content fingerprint
# ---------------------------------------------------------------------------

def _compute_fingerprint(text: str | None) -> list[str] | None:
    """Return top-50 3-word shingles from text as a near-dup fingerprint."""
    if not text or len(text) < 100:
        return None
    words = re.sub(r"[^a-z0-9\s]", "", text.lower()).split()
    shingles = [" ".join(words[i : i + 3]) for i in range(len(words) - 2)]
    if not shingles:
        return None
    top = [s for s, _ in Counter(shingles).most_common(50)]
    return top



# ---------------------------------------------------------------------------
# P2-2: Near-duplicate detection
# ---------------------------------------------------------------------------

async def _find_near_duplicate(
    session: AsyncSession,
    fingerprint: list[str] | None,
    url: str,
) -> str | None:
    """Return URL of a near-duplicate if one exists (>70% shingle overlap). Returns None if no dup."""
    if not fingerprint or len(fingerprint) < 10:
        return None
    try:
        # Find items with overlapping fingerprints using JSONB containment
        # This is approximate: check if at least 5 shingles from our fingerprint
        # appear in existing items' fingerprints
        # Use a simpler approach: check content_hash first, then sample fingerprints
        sample = fingerprint[:10]  # check top 10 shingles
        result = await session.execute(
            text("""
                SELECT url, content_fingerprint
                FROM crawled_items
                WHERE url != :url
                  AND content_fingerprint IS NOT NULL
                  AND content_fingerprint ?| :sample::text[]
                LIMIT 5
            """),
            {"url": url, "sample": sample},
        )
        rows = result.fetchall()
        for dup_url, dup_fp in rows:
            if isinstance(dup_fp, list):
                overlap = len(set(fingerprint) & set(dup_fp))
                similarity = overlap / len(set(fingerprint) | set(dup_fp))
                if similarity > 0.70:
                    return dup_url
    except Exception as e:
        logger.debug("Near-dup check failed (non-critical): %s", e)
    return None


# ---------------------------------------------------------------------------
# Main ingest function
# ---------------------------------------------------------------------------

async def save_items(
    items: list[CollectedItem],
    surface_key: str,
    session: AsyncSession,
) -> int:
    """Upsert items into crawled_items. Returns count of newly inserted rows."""
    if not items:
        return 0

    surface = await session.get(Surface, surface_key)

    inserted = 0
    for item in items:
        title = (item.get("title") or "").strip()
        # P2-C: strip fragment then normalise tracking params
        raw_url, _ = urldefrag((item.get("url") or "").strip())
        url = _normalize_url(raw_url)
        if not title or not url:
            continue

        published_at_val: datetime | None = None
        raw_date = item.get("published_at")
        if raw_date:
            try:
                from dateutil.parser import parse as parse_date  # type: ignore
                published_at_val = parse_date(raw_date)
            except Exception:
                try:
                    published_at_val = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                except Exception:
                    pass

        # P2-E: quality gate — discard junk content_body but keep metadata
        content_body = item.get("content_body")
        if not _passes_quality_gate(content_body):
            content_body = None

        content_hash = _hash_content(content_body)
        source_type = surface.source_type if surface else None

        # P2-A: language detection
        lang = _detect_lang(content_body or item.get("title"))

        # P2-F: staleness
        is_stale = _compute_is_stale(published_at_val)

        # P3-C: evidence level
        evidence_level = _infer_evidence_level(
            title,
            item.get("description"),
            source_type,
        )

        # P3-B: content fingerprint
        content_fingerprint = _compute_fingerprint(content_body)

        # P2-2: near-duplicate detection
        near_duplicate_of = await _find_near_duplicate(session, content_fingerprint, url)
        if near_duplicate_of:
            logger.info(
                "Near-duplicate detected: url=%s is dup of %s (Jaccard > 0.70)",
                url, near_duplicate_of,
            )

        row = {
            "external_id": item.get("external_id"),
            "source": item.get("source", "unknown"),
            "surface_key": surface_key,
            "title": title,
            "url": url,
            "description": item.get("description"),
            "content_body": content_body,
            "author": item.get("author"),
            "authors_json": item.get("authors_json"),
            "published_at": published_at_val,
            "collected_at": datetime.now(tz=timezone.utc),
            "rank_position": item.get("rank_position"),
            "engagement": item.get("engagement") or {},
            "doi": item.get("doi"),
            "journal": item.get("journal"),
            "open_access": item.get("open_access"),
            "authority_tier": surface.authority_tier if surface else None,
            "source_type": source_type,
            "audience_type": surface.audience_type if surface else None,
            "content_hash": content_hash,
            "content_updated_at": datetime.now(tz=timezone.utc) if content_body else None,
            "raw_payload": item.get("raw_payload") or {},
            "lang": lang,
            "is_stale": is_stale,
            "evidence_level": evidence_level,
            "content_fingerprint": content_fingerprint,
            "near_duplicate_of": near_duplicate_of,
        }

        stmt = insert(CrawledItem).values(**row)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_crawled_items_url",
            set_={
                "engagement": stmt.excluded.engagement,
                "rank_position": stmt.excluded.rank_position,
                "description": stmt.excluded.description,
                "content_body": stmt.excluded.content_body,
                "content_hash": stmt.excluded.content_hash,
                "content_updated_at": stmt.excluded.content_updated_at,
                "collected_at": stmt.excluded.collected_at,
                "authority_tier": stmt.excluded.authority_tier,
                "source_type": stmt.excluded.source_type,
                "audience_type": stmt.excluded.audience_type,
                "lang": stmt.excluded.lang,
                "is_stale": stmt.excluded.is_stale,
                "evidence_level": stmt.excluded.evidence_level,
                "content_fingerprint": stmt.excluded.content_fingerprint,
                "near_duplicate_of": stmt.excluded.near_duplicate_of,
                # P1-C: set needs_rechunk=True when content changes (handled below via SQL)
            },
        )

        try:
            result = await session.execute(stmt)
            # rowcount == 1 on insert, 0 on update (DO UPDATE with no change)
            if result.rowcount == 1:
                inserted += 1
        except IntegrityError:
            # DOI unique constraint conflict (same DOI at different URL)
            await session.rollback()
            logger.debug("DOI conflict skipped for url=%s doi=%s", url, item.get("doi"))
            continue

    try:
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.error("pipeline commit failed: %s", exc)
        return 0

    # --- Post-commit enrichment passes ---

    # P3-C re-embedding trigger — null out embeddings for records where content changed
    urls_with_content = [
        _normalize_url(urldefrag((item.get("url") or "").strip())[0])
        for item in items
        if item.get("content_body") and (item.get("url") or "").strip()
    ]
    if urls_with_content:
        try:
            await session.execute(
                text("""
                    UPDATE crawled_items
                    SET embedding = NULL,
                        embedded_at = NULL
                    WHERE url = ANY(:urls)
                      AND embedded_at IS NOT NULL
                      AND content_hash IS NOT NULL
                      AND content_hash != encode(
                            sha256(content_body::bytea), 'hex'
                          )
                """),
                {"urls": urls_with_content},
            )
            # P1-C: set needs_rechunk=True when content changes
            await session.execute(
                text("""
                    UPDATE crawled_items
                    SET needs_rechunk = TRUE
                    WHERE url = ANY(:urls)
                      AND content_hash IS NOT NULL
                      AND content_hash != encode(
                            sha256(content_body::bytea), 'hex'
                          )
                """),
                {"urls": urls_with_content},
            )
            await session.commit()
        except Exception as exc:
            logger.debug("Re-embedding/rechunk trigger failed (non-critical): %s", exc)

    return inserted


def _hash_content(content: str | None) -> str | None:
    """SHA-256 hash of content_body for change detection on re-crawl."""
    if not content:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Unpaywall enrichment
# ---------------------------------------------------------------------------

async def enrich_unpaywall(session: AsyncSession, batch_size: int = 50) -> int:
    """Fetch open-access URLs from Unpaywall for records with DOI.

    Also backfills oa_url for existing open_access=True records that
    have no oa_url set (records collected before migration 0004).
    """
    from src.config import settings
    from src.http.client import get_shared_client

    client = get_shared_client()
    updated = 0

    # Find records with DOI that need Unpaywall enrichment:
    # - records not yet checked (open_access IS NULL), OR
    # - records already marked OA but missing oa_url (pre-migration backfill)
    result = await session.execute(
        select(CrawledItem.id, CrawledItem.doi)
        .where(CrawledItem.doi.isnot(None))
        .where(
            (CrawledItem.open_access.is_(None)) |
            (CrawledItem.open_access.is_(True) & CrawledItem.oa_url.is_(None))
        )
        .limit(batch_size)
    )
    rows = result.fetchall()

    for row_id, doi in rows:
        api_url = f"https://api.unpaywall.org/v2/{doi}?email={settings.CRAWLER_EMAIL}"
        try:
            resp = await client.get(api_url)
            data = resp.json()
        except Exception as exc:
            logger.debug("Unpaywall failed for doi=%s: %s", doi, exc)
            continue

        is_oa = data.get("is_oa", False)
        best_oa = data.get("best_oa_location") or {}
        oa_url = best_oa.get("url_for_pdf") or best_oa.get("url")

        if is_oa or oa_url:
            await session.execute(
                update(CrawledItem)
                .where(CrawledItem.id == row_id)
                .values(open_access=is_oa, oa_url=oa_url)  # persist oa_url
            )
            updated += 1

    await session.commit()
    return updated


async def enrich_fulltext(session: AsyncSession, batch_size: int = 20) -> int:
    """Fetch full text for open-access records that have an oa_url.

    Phase 1: HTML URLs only. PDF URLs are deferred (logged + skipped) until
    P3-A PDF extractor is available.
    """
    from src.http.client import get_shared_client
    from src.extractors.html import extract_body
    from bs4 import BeautifulSoup

    client = get_shared_client()
    enriched = 0

    result = await session.execute(
        select(CrawledItem.id, CrawledItem.oa_url)
        .where(CrawledItem.open_access.is_(True))
        .where(CrawledItem.content_body.is_(None))
        .where(CrawledItem.doi.isnot(None))
        .where(CrawledItem.oa_url.isnot(None))
        .limit(batch_size)
    )
    rows = result.fetchall()

    for row_id, oa_url in rows:
        # PDF detection
        is_pdf = (
            oa_url.lower().endswith(".pdf") or
            "pdf" in oa_url.lower()
        )
        if is_pdf:
            try:
                from src.extractors.pdf import extract_text_from_pdf
                resp = await client.get(oa_url)
                if "application/pdf" in resp.headers.get("content-type", ""):
                    extracted = extract_text_from_pdf(resp.content)
                    if extracted is not None:  # '' = permanent failure sentinel, also stored
                        await session.execute(
                            update(CrawledItem)
                            .where(CrawledItem.id == row_id)
                            .values(content_body=extracted)  # store '' to mark permanent failure
                        )
                        enriched += 1
                    continue
                # Fall through to HTML extraction if content-type not PDF
            except ImportError:
                logger.info("enrich_fulltext: PDF oa_url deferred (pdfplumber not installed): %s", oa_url)
                continue
            except Exception as exc:
                logger.debug("enrich_fulltext: PDF fetch failed for %s: %s", oa_url, exc)
                continue

        # HTML fetch
        try:
            resp = await client.get(oa_url, use_browser_ua=True)
            soup = BeautifulSoup(resp.text, "html.parser")
            body = extract_body(soup)

            # Quality gate for paywall masquerading as OA
            if body is None:
                logger.debug("enrich_fulltext: empty/low-quality body at %s", oa_url)
                continue

            paywall_signals = [
                "sign in", "log in", "create account", "access denied", "subscribe to read"
            ]
            body_lower = body.lower()
            if len(body) < 300 or any(s in body_lower for s in paywall_signals):
                logger.debug("enrich_fulltext: paywall detected at %s — discarding", oa_url)
                continue

            await session.execute(
                update(CrawledItem)
                .where(CrawledItem.id == row_id)
                .values(content_body=body)
            )
            enriched += 1

        except Exception as exc:
            logger.debug("enrich_fulltext: fetch failed for %s: %s", oa_url, exc)
            continue

    await session.commit()
    return enriched


async def enrich_fulltext_loop() -> None:
    """Long-running loop: enriches academic records with full text every 6 hours.

    Step 1 — enrich_unpaywall: for every item with a DOI, fetch its open-access
              URL from Unpaywall and store it in oa_url.
    Step 2 — enrich_fulltext: for every item with oa_url set, fetch and store
              the actual HTML/PDF content into content_body.

    Both steps must run in order — fulltext has nothing to fetch until
    Unpaywall has populated oa_url.
    """
    import asyncio as _asyncio
    _interval = 6 * 3600
    logger.info("enrich_fulltext loop started (interval=%ds)", _interval)
    while True:
        try:
            # Step 1: resolve OA URLs for items with DOI
            async with AsyncSessionLocal() as session:
                oa_count = await enrich_unpaywall(session)
                if oa_count:
                    logger.info("enrich_unpaywall: resolved %d OA URLs", oa_count)

            # Step 2: fetch full text for items that now have an OA URL
            async with AsyncSessionLocal() as session:
                ft_count = await enrich_fulltext(session)
                if ft_count:
                    logger.info("enrich_fulltext: enriched %d records with full text", ft_count)
        except Exception as exc:
            logger.error("enrich_fulltext loop error: %s", exc)
        await _asyncio.sleep(_interval)
