"""Ingest pipeline: upserts CollectedItems into the DB and enriches with Unpaywall."""
from __future__ import annotations

import hashlib
import logging
import re
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urldefrag

from sqlalchemy import select, update, text, case, desc, cast, String, func
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

# Platforms whose content type is known from the platform itself, independent
# of any title/description keyword match — checked after the keyword scan
# above but before the generic source_type fallback below. Added alongside
# the sleep/eating/adhd expansion because most government/hospital pages
# (CDC, NHS, Mayo, ...) never contain phrases like "systematic review" or
# "RCT", so the keyword-only inference left them with evidence_level=None.
_PLATFORM_EVIDENCE_LEVEL: dict[str, str] = {
    "biorxiv": "preprint",           # bioRxiv/medRxiv preprints, by construction
    "clinicaltrials": "clinical_trial",
}


def _infer_evidence_level(
    title: str,
    description: str | None,
    source_type: str | None,
    platform: str | None = None,
) -> str | None:
    if source_type in ("forum", "reddit", "social"):
        return "anecdotal"
    if source_type == "blog":
        return "blog"
    text = ((title or "") + " " + (description or "")).lower()
    for level, keywords in _EVIDENCE_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return level
    if platform in _PLATFORM_EVIDENCE_LEVEL:
        return _PLATFORM_EVIDENCE_LEVEL[platform]
    if source_type == "official_health" and platform in ("html_crawl", "playwright_crawl", "nhs_api"):
        return "government_guidance"
    if source_type == "hospital":
        return "hospital_education"
    if source_type == "academic":
        # Academic-API content (PubMed/EuropePMC/Semantic Scholar/CrossRef/
        # DOAJ/OpenAlex/CORE) is peer-reviewed journal literature by
        # definition once preprints and trial registrations are excluded
        # above. Supersedes the old source_type=="peer_reviewed" branch
        # below, which nothing in surfaces.json ever actually set.
        return "peer_reviewed_study"
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
        # Wrap in a savepoint so that any DB error (e.g. type mismatch, operator
        # issue) only rolls back this nested transaction — NOT the outer batch.
        # Without this, a failed query here would put the asyncpg connection into
        # "InFailedSQLTransactionError" state and cause every subsequent INSERT to
        # fail until the outer transaction is rolled back.
        async with session.begin_nested():
            sample = fingerprint[:10]  # check top 10 shingles
            # Use jsonb_array_elements_text + ANY to avoid the ?| operator.
            # The ?| / ?& JSONB operators conflict with SQLAlchemy/asyncpg
            # parameter handling (? is treated as a positional placeholder),
            # causing the query to fail and abort the outer transaction.
            result = await session.execute(
                text("""
                    SELECT url, content_fingerprint
                    FROM crawled_items
                    WHERE url != :url
                      AND content_fingerprint IS NOT NULL
                      AND EXISTS (
                          SELECT 1
                          FROM jsonb_array_elements_text(content_fingerprint) AS elem
                          WHERE elem = ANY(:sample::text[])
                      )
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
# Multi-domain tag merge (sleep/eating/adhd expansion)
# ---------------------------------------------------------------------------

async def _merge_tag_list(
    session: AsyncSession,
    column: str,
    url: str,
    new_tags: list[str] | None,
) -> list[str]:
    """Union new_tags with whatever is already stored in `column` for `url`.

    Same content item can legitimately be reached by more than one surface
    (e.g. an autism+ADHD comorbidity paper matches both pubmed_autism and
    pubmed_adhd) and should keep every domain it was matched under, not just
    whichever surface's crawl happened to upsert it first. Done as a Python-side
    SELECT + set-union — matching this codebase's existing style in
    _find_near_duplicate() above, whose own comments note that JSONB ?| / ?&
    operators are incompatible with asyncpg's parameter binding — rather than
    a SQL-side JSONB expression in the ON CONFLICT clause.
    """
    new_set = set(new_tags or [])
    if not url:
        return sorted(new_set)
    try:
        # BUG FIXED 2026-08-27: this is called TWICE per item in save_items()'s
        # main loop (once for domain_tags, once for topic_tags) — the busiest
        # call site in the whole ingest path. It caught its own exceptions but
        # never rolled back, so any failure here left the session's underlying
        # transaction ABORTED while Python-level control flow continued as if
        # nothing happened; every subsequent INSERT in the same batch then
        # failed too, with a confusing unrelated error (the exact
        # "InFailedSQLTransactionError" failure mode already root-caused once
        # for clinicaltrials_* — see save_items()'s re-embedding trigger fix).
        # A SAVEPOINT here contains any failure to just this one lookup.
        async with session.begin_nested():
            result = await session.execute(
                text(f"SELECT {column} FROM crawled_items WHERE url = :url"),
                {"url": url},
            )
            row = result.first()
    except Exception as exc:
        logger.debug("_merge_tag_list(%s) lookup failed (non-critical): %s", column, exc)
        return sorted(new_set)
    existing = set(row[0]) if row and row[0] else set()
    return sorted(existing | new_set)


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

    # Extract surface attributes into locals BEFORE the loop.
    # session.rollback() (called on IntegrityError) expires all ORM objects, so
    # accessing surface.xxx inside the loop after a rollback would trigger a
    # synchronous lazy-load in an async context → MissingGreenlet crash.
    _source_type: str | None = surface.source_type if surface else None
    _authority_tier: int | None = surface.authority_tier if surface else None
    _audience_type: str | None = surface.audience_type if surface else None
    _platform: str | None = surface.platform if surface else None
    _domain_tags: list[str] | None = surface.domain_tags if surface else None
    _topic_tags: list[str] | None = surface.topic_tags if surface else None

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
        # Strip NUL bytes — Postgres UTF8 text columns reject them outright
        # (CharacterNotInRepertoireError). Any collector that scrapes a live
        # page/PDF can hit this (confirmed for enrich_fulltext's PDF parsing
        # — see _sanitize() there — but html_crawl/playwright_crawl/sitemap/
        # rss all feed content_body through this SAME path too), and unlike
        # enrich_fulltext this loop only caught IntegrityError, not a general
        # DBAPIError — so this would have silently discarded the ENTIRE
        # batch of items from this surface's poll, not just the bad one.
        if content_body:
            content_body = content_body.replace("\x00", "")
        if not _passes_quality_gate(content_body):
            content_body = None

        content_hash = _hash_content(content_body)
        source_type = _source_type

        # P2-A: language detection
        lang = _detect_lang(content_body or item.get("title"))

        # P2-F: staleness
        is_stale = _compute_is_stale(published_at_val)

        # P3-C: evidence level
        evidence_level = _infer_evidence_level(
            title,
            item.get("description"),
            source_type,
            _platform,
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

        # Sleep/eating/adhd expansion: union this surface's domain_tags/
        # topic_tags with whatever is already stored for this URL, so an
        # item matched by more than one surface (e.g. autism+ADHD comorbidity
        # research) keeps every domain it belongs to.
        domain_tags = await _merge_tag_list(session, "domain_tags", url, _domain_tags)
        topic_tags = await _merge_tag_list(session, "topic_tags", url, _topic_tags)

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
            "authority_tier": _authority_tier,
            "source_type": source_type,
            "audience_type": _audience_type,
            "content_hash": content_hash,
            "content_updated_at": datetime.now(tz=timezone.utc) if content_body else None,
            "raw_payload": item.get("raw_payload") or {},
            "lang": lang,
            "is_stale": is_stale,
            "evidence_level": evidence_level,
            "content_fingerprint": content_fingerprint,
            "near_duplicate_of": near_duplicate_of,
            "domain_tags": domain_tags,
            "topic_tags": topic_tags,
        }

        stmt = insert(CrawledItem).values(**row)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_crawled_items_url",
            set_={
                "engagement": stmt.excluded.engagement,
                "rank_position": stmt.excluded.rank_position,
                "description": stmt.excluded.description,
                # BUG FIXED 2026-08-27: these three used to be a bare
                # overwrite with stmt.excluded.*. Most academic collectors
                # (pubmed/crossref/europepmc/openalex/doaj/semanticscholar/
                # biorxiv/core) set content_body=None at collection time —
                # full text is only filled in LATER by enrich_fulltext().
                # If the SAME surface later re-discovers the same URL (a
                # normal, common occurrence — polling the same query finds
                # overlapping results), the new row's content_body is None
                # again, and the bare overwrite silently WIPED OUT whatever
                # real full text enrich_fulltext had already fetched,
                # putting a real, downloaded article back to square one
                # (and, if that domain had since been given up on, losing
                # it permanently). Confirmed live: option 10's total
                # full-article count visibly dropped between two checks a
                # few minutes apart with no corresponding code change.
                # COALESCE keeps the existing value whenever the new
                # collection pass didn't actually find anything —  a
                # re-collect can still legitimately UPDATE content (e.g. a
                # changed guideline page), it just can't blank it out.
                "content_body": func.coalesce(stmt.excluded.content_body, CrawledItem.content_body),
                "content_hash": func.coalesce(stmt.excluded.content_hash, CrawledItem.content_hash),
                "content_updated_at": func.coalesce(stmt.excluded.content_updated_at, CrawledItem.content_updated_at),
                "collected_at": stmt.excluded.collected_at,
                "authority_tier": stmt.excluded.authority_tier,
                "source_type": stmt.excluded.source_type,
                "audience_type": stmt.excluded.audience_type,
                "lang": stmt.excluded.lang,
                "is_stale": stmt.excluded.is_stale,
                "evidence_level": stmt.excluded.evidence_level,
                "content_fingerprint": stmt.excluded.content_fingerprint,
                "near_duplicate_of": stmt.excluded.near_duplicate_of,
                # Already the union of the existing row + this surface's tags
                # (computed pre-upsert by _merge_tag_list) — not a plain
                # overwrite. See _merge_tag_list's docstring.
                "domain_tags": stmt.excluded.domain_tags,
                "topic_tags": stmt.excluded.topic_tags,
                # P1-C: set needs_rechunk=True when content changes (handled below via SQL)
            },
        )

        try:
            # BUG FIXED 2026-08-27: was `except IntegrityError: await
            # session.rollback()` — a bare (non-nested) rollback() here
            # discards the WHOLE transaction, not just this one item. Since
            # this loop shares one session across the whole batch (committed
            # once at the end), one item hitting a DOI conflict wiped out
            # every OTHER item already upserted earlier in the same loop
            # iteration too, not just itself. Also widened from
            # IntegrityError-only to any DBAPIError, since a stray NUL byte
            # (or other encoding oddity) that slips past the sanitization
            # above raises a different exception class and was previously
            # uncaught here — able to abort the session for every item
            # still to come in this same batch.
            async with session.begin_nested():
                result = await session.execute(stmt)
                # rowcount == 1 on insert, 0 on update (DO UPDATE with no change)
                if result.rowcount == 1:
                    inserted += 1
        except IntegrityError:
            # DOI unique constraint conflict (same DOI at different URL)
            logger.debug("DOI conflict skipped for url=%s doi=%s", url, item.get("doi"))
            continue
        except Exception as exc:
            logger.warning("save_items: upsert failed for url=%s, skipping just this item: %s", url, exc)
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
            # This IS genuinely non-critical (content just won't be flagged
            # for re-embedding this round) — BUT without the rollback, a
            # failed statement here leaves the session's transaction in an
            # ABORTED state, and since save_items() returns normally after
            # this (no re-raise), that poisoned session gets handed straight
            # back to the caller. The next unrelated statement on it then
            # fails too with "current transaction is aborted" — which is
            # exactly the clinicaltrials_* failure signature this was traced
            # from (scheduler.py's own surfaces-table UPDATE failing right
            # after save_items() returns, with no error of its own).
            await session.rollback()
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

async def enrich_unpaywall(session: AsyncSession, batch_size: int = 300) -> int:
    """Fetch open-access URLs from Unpaywall for records with DOI.

    Also backfills oa_url for existing open_access=True records that
    have no oa_url set (records collected before migration 0004).

    Fetches the whole batch concurrently (was a plain sequential loop) —
    api.unpaywall.org now has its own DOMAIN_RATE_LIMITS entry (60 rpm,
    was silently defaulting to 20 rpm) in src/http/client.py, and this is
    a single lightweight JSON call per DOI with no CPU-bound work, so
    concurrency alone (no process pool needed, unlike enrich_fulltext's
    PDF parsing) is enough to actually use that higher rate. Real
    measured duration for batch_size=300 was already ~7-8 min even
    sequentially at the old 20rpm cap — this is meant to bring that
    down, not to push total throughput past what Unpaywall tolerates.
    """
    import asyncio
    from src.config import settings
    from src.http.client import get_shared_client

    client = get_shared_client()
    updated = 0

    # Find records with DOI that need Unpaywall enrichment:
    # - records not yet checked (open_access IS NULL), OR
    # - records already marked OA but missing oa_url (pre-migration backfill)
    # Priority: sleep/eating records go first (product priority), then newest first —
    # otherwise this FIFOs through the oldest backlog from other domains and never
    # catches up to recently-collected sleep/eating items.
    _priority = case(
        (cast(CrawledItem.domain_tags, String).ilike("%sleep%"), 0),
        (cast(CrawledItem.domain_tags, String).ilike("%eating%"), 0),
        else_=1,
    )
    result = await session.execute(
        select(CrawledItem.id, CrawledItem.doi)
        .where(CrawledItem.doi.isnot(None))
        .where(
            (CrawledItem.open_access.is_(None)) |
            (CrawledItem.open_access.is_(True) & CrawledItem.oa_url.is_(None))
        )
        .order_by(_priority, desc(CrawledItem.collected_at))
        .limit(batch_size)
    )
    rows = result.fetchall()
    if not rows:
        return 0

    async def _check(row_id, doi):
        api_url = f"https://api.unpaywall.org/v2/{doi}?email={settings.CRAWLER_EMAIL}"
        try:
            resp = await client.get(api_url)
            return (row_id, resp.json(), None)
        except Exception as exc:
            return (row_id, None, exc)

    results = await asyncio.gather(*(_check(rid, doi) for rid, doi in rows))

    # Sequential writes (AsyncSession isn't safe for concurrent use), each in
    # its own SAVEPOINT so one malformed response can't poison the rest of
    # the batch's writes — same pattern/rationale as enrich_fulltext's _write.
    async def _write(row_id, is_oa, oa_url):
        try:
            async with session.begin_nested():
                await session.execute(
                    update(CrawledItem).where(CrawledItem.id == row_id)
                    .values(open_access=is_oa, oa_url=oa_url)
                )
        except Exception as exc:
            logger.warning("enrich_unpaywall: write failed for row %s, skipping just this row: %s", row_id, exc)

    for row_id, data, exc in results:
        if data is None:
            logger.debug("Unpaywall failed for row=%s: %s", row_id, exc)
            continue

        is_oa = data.get("is_oa", False)
        best_oa = data.get("best_oa_location") or {}
        oa_url = best_oa.get("url_for_pdf") or best_oa.get("url")

        # Always persist the answer, even a negative one (is_oa=False, no oa_url).
        # Previously this only wrote on a positive result, so every non-OA DOI
        # (the majority of academic papers) stayed open_access=NULL forever and
        # kept matching this query's WHERE clause — re-checked every single cycle,
        # starving new records out of the batch. That was the main reason the
        # backlog never shrank despite running every 6 hours.
        await _write(row_id, is_oa, oa_url)
        if is_oa or oa_url:
            updated += 1

    await session.commit()
    return updated


_pdf_pool = None  # lazy singleton — see _get_pdf_pool()


def _get_pdf_pool():
    """Return the shared PDF-parsing process pool, creating it on first use.

    Deliberately lazy (not created at module import time): importing
    src.pipeline happens from tests, admin tooling, and migrations too —
    none of those need 4 worker processes spawned just because the module
    was imported. Created once and reused across every enrich_fulltext()
    call for the life of the crawler process (unlike embed_worker, which is
    deliberately a fresh subprocess per run to release ML-model memory —
    these workers are lightweight and should stay warm; respawning a
    process pool every 30 min would just add startup overhead for nothing).

    Sized at 4 workers on this machine's 8 cores — not 8 — to leave real
    headroom for the scheduler's concurrent surface collectors,
    chunk_pipeline, and the embed_worker subprocess. See crawl.txt section 14.
    """
    global _pdf_pool
    if _pdf_pool is None:
        from concurrent.futures import ProcessPoolExecutor
        logger.info("Spawning PDF-parsing process pool (4 workers)")
        _pdf_pool = ProcessPoolExecutor(max_workers=4)
    return _pdf_pool


def shutdown_pdf_pool() -> None:
    """Cleanly terminate the PDF-parsing worker processes on exit.

    Without calling this, killing the crawler's main process does NOT kill
    its ProcessPoolExecutor children — they get re-parented to PID 1 and
    keep running indefinitely, each holding real memory. This bit the
    machine directly on 2026-08-26: 3 stop/restart cycles during
    development left 15 orphaned worker processes running (from 02:04,
    02:35, 02:50), consuming most of the machine's free RAM. src/main.py
    calls this on SIGTERM/SIGINT so a normal restart cleans up after itself
    (a bare `kill -9` still bypasses it — there's no way to intercept that).
    """
    global _pdf_pool
    if _pdf_pool is not None:
        logger.info("Shutting down PDF-parsing process pool")
        _pdf_pool.shutdown(wait=False, cancel_futures=True)
        _pdf_pool = None


def _parse_html_body(html_text: str) -> str | None:
    """Run in a worker thread — BeautifulSoup + extract_body for one page.

    Cheap enough per-item (unlike multi-page PDF parsing) that a thread is
    sufficient here; no need to burn a process-pool slot on it too.
    """
    from bs4 import BeautifulSoup
    from src.extractors.html import extract_body
    return extract_body(BeautifulSoup(html_text, "html.parser"))


_GIVE_UP_403_THRESHOLD = 5  # matches src/http/client.py's CircuitBreaker.threshold


def _domain_of(url: str) -> str:
    """Same normalization as src/http/client.py's _domain() — strip a leading
    'www.' so 'www.mdpi.com' and 'mdpi.com' are tracked as the same domain."""
    from urllib.parse import urlparse
    netloc = urlparse(url).netloc
    return netloc[4:] if netloc.startswith("www.") else netloc


class _DomainGivenUp(Exception):
    """Raised by _fetch() instead of making a request, when the domain has
    already crossed _GIVE_UP_403_THRESHOLD consecutive 403s across past
    enrich_fulltext() cycles (see blocked_domains table / migration 0021).
    Distinguished from a real fetch failure so stage 3 writes the ''
    permanent-failure sentinel immediately instead of leaving NULL (which
    would just retry — and re-fail — forever)."""


async def enrich_fulltext(session: AsyncSession, batch_size: int = 300) -> int:
    """Fetch full text for open-access records that have an oa_url.

    Three stages, run across the whole batch instead of one row at a time:
      1. DOWNLOAD (I/O-bound) — fetch every URL in the batch concurrently.
         Cheap on CPU; the existing per-domain semaphore in
         src/http/client.py already caps concurrency to any one site, so
         doing all of these at once doesn't need to change anything there.
      2. PARSE (CPU-bound) — PDFs go to a process pool (real multi-core
         parallelism, since pdfplumber parsing doesn't release the GIL);
         HTML pages go to a thread (cheap enough, no need for a process
         slot). Both run concurrently with each other and with stage 1's
         tail end.
      3. WRITE — a single sequential pass over the results, applying the
         same permanent-failure-sentinel rules as before (store '' for a
         definitive rejection so it's never re-fetched; leave NULL on a
         transient fetch error so it's retried next cycle). AsyncSession
         isn't safe for concurrent use, so this stage is intentionally
         sequential even though 1 and 2 are not.

    See crawl.txt section 14 for the full design writeup.
    """
    import asyncio
    from src.http.client import get_shared_client
    from src.extractors.pdf import extract_text_from_pdf
    from src.storage.models import BlockedDomain

    client = get_shared_client()
    enriched = 0

    given_up_domains = set(
        (await session.execute(
            select(BlockedDomain.domain).where(BlockedDomain.given_up.is_(True))
        )).scalars().all()
    )

    _priority = case(
        (cast(CrawledItem.domain_tags, String).ilike("%sleep%"), 0),
        (cast(CrawledItem.domain_tags, String).ilike("%eating%"), 0),
        else_=1,
    )
    result = await session.execute(
        select(CrawledItem.id, CrawledItem.oa_url)
        .where(CrawledItem.open_access.is_(True))
        .where(CrawledItem.content_body.is_(None))
        .where(CrawledItem.doi.isnot(None))
        .where(CrawledItem.oa_url.isnot(None))
        .order_by(_priority, desc(CrawledItem.collected_at))
        .limit(batch_size)
    )
    rows = result.fetchall()
    if not rows:
        return 0

    # Instrumentation added 2026-08-27: the only prior visibility into this
    # function was one summary line ("enriched N records") logged AFTER the
    # entire batch finished — no way to tell whether successes landed in the
    # first few minutes and the rest of the cycle (5-25 min, measured) was
    # dead weight, or were spread through the whole thing. httpx's own
    # per-request log lines don't help either — they're not tagged as
    # belonging to this function, and are interleaved with every other
    # concurrent surface's requests. This logs real-time progress so that
    # question is actually answerable from crawler.log going forward.
    import time as _time
    batch_start = _time.monotonic()
    logger.info(
        "enrich_fulltext: starting batch of %d items (%d domains already given up, "
        "skipped without a request)",
        len(rows), len(given_up_domains),
    )
    _progress = {"done": 0, "success": 0, "given_up_skip": 0, "failed": 0}

    # ── Stage 1: concurrent download ──────────────────────────────────────
    async def _fetch(row_id, oa_url):
        try:
            if _domain_of(oa_url) in given_up_domains:
                _progress["given_up_skip"] += 1
                return (row_id, oa_url, None, _DomainGivenUp(_domain_of(oa_url)))
            try:
                resp = await client.get(oa_url, use_browser_ua=True)
                _progress["success" if resp.status_code < 400 else "failed"] += 1
                return (row_id, oa_url, resp, None)
            except Exception as exc:
                _progress["failed"] += 1
                return (row_id, oa_url, None, exc)
        finally:
            _progress["done"] += 1
            # Every 25 completions (not every single one — 300/cycle would be
            # too noisy), log a timestamped snapshot of how far through the
            # batch we are and how the outcomes are splitting so far. This is
            # what actually answers "front-loaded or spread out": compare the
            # elapsed-seconds column across consecutive lines.
            if _progress["done"] % 25 == 0 or _progress["done"] == len(rows):
                elapsed = _time.monotonic() - batch_start
                logger.info(
                    "enrich_fulltext: progress %d/%d done at %.0fs elapsed "
                    "(success=%d given_up_skip=%d failed=%d)",
                    _progress["done"], len(rows), elapsed,
                    _progress["success"], _progress["given_up_skip"], _progress["failed"],
                )

    fetched = await asyncio.gather(*(_fetch(rid, url) for rid, url in rows))
    logger.info(
        "enrich_fulltext: all %d fetches done after %.0fs — parsing + writing next",
        len(rows), _time.monotonic() - batch_start,
    )

    # ── Track consecutive 403s per domain, across this batch AND prior
    #    cycles (the counter is persisted in blocked_domains) ──────────────
    # A domain that already got 5+ 403s in a row here has almost certainly
    # got an active anti-bot WAF (Cloudflare/Akamai/etc — confirmed durable
    # even against a real browser for a comparable domain, see chop_adhd's
    # content_notes) rather than a transient blip, so give up on it for good
    # instead of re-attempting it, and everything on it, every cycle forever.
    # Attribute by the FINAL url (after any redirects), not the requested
    # oa_url — many oa_urls are doi.org resolver links that redirect to the
    # actual publisher; blaming doi.org for a 403 that really came from
    # whatever it redirected to would give up on the shared front door for
    # EVERY publisher's DOIs, including ones that were never blocking us at
    # all. src/http/client.py attaches final_url to the exception for this.
    domain_403_count: dict[str, int] = {}
    domain_succeeded: set[str] = set()
    for row_id, oa_url, resp, fetch_exc in fetched:
        if isinstance(fetch_exc, PermissionError) and "403" in str(fetch_exc):
            domain = _domain_of(getattr(fetch_exc, "final_url", None) or oa_url)
            domain_403_count[domain] = domain_403_count.get(domain, 0) + 1
        elif resp is not None:
            domain_succeeded.add(_domain_of(str(resp.url)))

    newly_given_up: set[str] = set()
    for domain in set(domain_403_count) | domain_succeeded:
        if domain in domain_succeeded:
            new_count = 0
        else:
            existing = (await session.execute(
                select(BlockedDomain.consecutive_403_count).where(BlockedDomain.domain == domain)
            )).scalar_one_or_none() or 0
            new_count = existing + domain_403_count.get(domain, 0)
        will_give_up = new_count >= _GIVE_UP_403_THRESHOLD
        stmt = insert(BlockedDomain).values(
            domain=domain,
            consecutive_403_count=new_count,
            given_up=will_give_up,
            given_up_at=datetime.now(tz=timezone.utc) if will_give_up else None,
            last_checked_at=datetime.now(tz=timezone.utc),
        ).on_conflict_do_update(
            index_elements=["domain"],
            set_={
                "consecutive_403_count": new_count,
                "given_up": will_give_up,
                "given_up_at": datetime.now(tz=timezone.utc) if will_give_up else None,
                "last_checked_at": datetime.now(tz=timezone.utc),
            },
        )
        await session.execute(stmt)
        if will_give_up and domain not in given_up_domains:
            newly_given_up.add(domain)
    if newly_given_up:
        logger.warning(
            "enrich_fulltext: giving up on domain(s) after %d+ consecutive 403s: %s",
            _GIVE_UP_403_THRESHOLD, sorted(newly_given_up),
        )

    # ── Stage 2: concurrent parse — PDFs on the process pool, HTML on threads ──
    loop = asyncio.get_running_loop()
    pdf_pool = _get_pdf_pool()
    parse_futures = []
    for row_id, oa_url, resp, fetch_exc in fetched:
        if resp is None:
            parse_futures.append(None)  # fetch failed — nothing to parse
            continue
        is_pdf = "application/pdf" in resp.headers.get("content-type", "")
        if is_pdf:
            parse_futures.append(
                loop.run_in_executor(pdf_pool, extract_text_from_pdf, resp.content)
            )
        else:
            parse_futures.append(asyncio.to_thread(_parse_html_body, resp.text))

    # Gather only the real futures, preserving position via a placeholder pass.
    real_futures = [f for f in parse_futures if f is not None]
    real_results = iter(
        await asyncio.gather(*real_futures, return_exceptions=True) if real_futures else []
    )
    parsed = [None if f is None else next(real_results) for f in parse_futures]

    async def _write(row_id, content_body_value):
        """Execute one UPDATE inside its own SAVEPOINT, isolated so a single
        bad row (e.g. a NUL byte from a PDF ligature mis-decode — Postgres's
        UTF8 text columns reject \\x00 outright) can't poison the whole
        session and silently wipe out every other write in this batch.

        Deliberately a SAVEPOINT (session.begin_nested()), NOT a plain
        session.rollback() — this whole batch shares one outer transaction
        that only gets commit()-ed once at the end, so a bare rollback()
        would discard every OTHER item's already-written update too, not
        just this failed one. A SAVEPOINT only undoes back to itself.

        Without this, one DBAPIError here would leave the AsyncSession in a
        failed-transaction state for the rest of the loop AND prevent the
        final commit() from ever running — the exact same failure mode
        already seen in the clinicaltrials_* surfaces (see earlier fix).
        batch_size=300 makes hitting this kind of row far more likely per
        cycle than it used to be at 100.
        """
        try:
            async with session.begin_nested():
                await session.execute(
                    update(CrawledItem).where(CrawledItem.id == row_id).values(content_body=content_body_value)
                )
        except Exception as exc:
            logger.warning("enrich_fulltext: write failed for row %s, skipping just this row: %s", row_id, exc)

    def _sanitize(text: str | None) -> str | None:
        """Strip NUL bytes — Postgres UTF8 text columns reject them outright
        (CharacterNotInRepertoireError), and pdfplumber occasionally emits one
        in place of an unrecognized ligature (e.g. the "fl" in "influence").
        """
        return text.replace("\x00", "") if text else text

    # ── Stage 3: sequential write — same rules as before, just reordered ──
    for (row_id, oa_url, resp, fetch_exc), parse_result in zip(fetched, parsed):
        if resp is None:
            # _DomainGivenUp is raised pre-fetch against the REQUESTED
            # domain (we never got far enough to see a redirect); a 403
            # this batch is checked against the FINAL domain (post-redirect)
            # to match how it was tallied above — see the attribution note
            # a few lines up.
            final_domain = _domain_of(getattr(fetch_exc, "final_url", None) or oa_url)
            # Domain already given up before this batch, OR just crossed the
            # threshold from 403s seen earlier IN this same batch — either
            # way, this is now a permanent verdict, not a transient failure:
            # write '' so it stops occupying a queue slot, instead of NULL
            # (which would just retry — and re-fail — every future cycle).
            # BUG FIXED 2026-08-27: this previously only checked
            # `final_domain in newly_given_up` (crossed the threshold THIS
            # cycle) — it forgot `given_up_domains` (crossed in an EARLIER
            # cycle). A row whose oa_url is a redirector (e.g. doi.org)
            # that lands on an already-given-up publisher was never caught
            # by the pre-fetch check (which only sees the requested domain,
            # doi.org, not the eventual destination) NOR by this one — so it
            # kept getting a real fetch attempt, a real 403, and was left
            # NULL forever, retried every single cycle indefinitely. Found
            # by watching a "given up" domain's consecutive_403_count keep
            # climbing (to 1370+) long after it was already given up.
            if (
                isinstance(fetch_exc, _DomainGivenUp)
                or final_domain in newly_given_up
                or final_domain in given_up_domains
            ):
                await _write(row_id, "")
                continue
            logger.debug("enrich_fulltext: fetch failed for %s: %s", oa_url, fetch_exc)
            continue  # transient — leave NULL, retry next cycle

        is_pdf = "application/pdf" in resp.headers.get("content-type", "")

        if isinstance(parse_result, Exception):
            logger.debug("enrich_fulltext: parse raised for %s: %s", oa_url, parse_result)
            continue  # transient/unexpected — leave NULL, retry next cycle

        if is_pdf:
            extracted = _sanitize(parse_result)
            # extracted is None only when pdfplumber raised while parsing this
            # exact PDF (locked/corrupt) — the same bytes will fail the same
            # way every time, so treat it as permanent too and store ''
            # instead of leaving NULL (which would re-download and re-parse
            # this same doomed PDF every cycle forever).
            await _write(row_id, extracted or "")
            if extracted:
                enriched += 1
            continue

        # HTML — quality gate for paywall masquerading as OA. These are
        # definitive, page-structure-level verdicts (not a transient network
        # hiccup), so persist '' as a permanent-failure sentinel — same
        # convention as the PDF branch above. Without this, a page that will
        # never pass the gate gets re-fetched and re-rejected every cycle
        # forever, burning batch slots that should go to unfetched records.
        body = _sanitize(parse_result)
        if body is None:
            logger.debug("enrich_fulltext: empty/low-quality body at %s", oa_url)
            await _write(row_id, "")
            continue

        paywall_signals = [
            "sign in", "log in", "create account", "access denied", "subscribe to read"
        ]
        body_lower = body.lower()
        if len(body) < 300 or any(s in body_lower for s in paywall_signals):
            logger.debug("enrich_fulltext: paywall detected at %s — discarding", oa_url)
            await _write(row_id, "")
            continue

        await _write(row_id, body)
        enriched += 1

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
    _interval = 30 * 60  # was 6h — Unpaywall/fetch throughput is bounded by the
    # per-domain semaphore (=3) in src/http/client.py, not by this loop's own
    # pacing, and downstream (chunking/embedding) has ample headroom — so cycle
    # much more often to burn down the backlog faster.
    logger.info("enrich_fulltext loop started (interval=%ds)", _interval)
    while True:
        try:
            # Step 1: resolve OA URLs for items with DOI
            async with AsyncSessionLocal() as session:
                oa_count = await enrich_unpaywall(session)
                # Always log, even 0 — setup.sh option 10 reads the LAST such
                # line to report "articles downloaded in the last cycle";
                # skipping the log on a zero result would make it silently
                # show a stale count from whichever earlier cycle last had
                # a nonzero result, instead of the true, most recent one.
                logger.info("enrich_unpaywall: resolved %d OA URLs", oa_count)

            # Step 2: fetch full text for items that now have an OA URL
            async with AsyncSessionLocal() as session:
                ft_count = await enrich_fulltext(session)
                logger.info("enrich_fulltext: enriched %d records with full text", ft_count)
        except Exception as exc:
            logger.error("enrich_fulltext loop error: %s", exc)
        await _asyncio.sleep(_interval)
