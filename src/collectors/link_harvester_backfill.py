"""Link Harvester Backfill: fetches real metadata for paper stubs created by link_harvester.

Reads stubs (source='link_harvester') and calls direct single-record API endpoints:
  - DOI stubs  → CrossRef /works/{doi}
  - PMID stubs → PubMed efetch?id={pmid}
  - PMC stubs  → EuropePMC /search?query=PMC{id}
  - arXiv stubs → deferred (no arXiv collector yet)
"""
from __future__ import annotations

import logging
from datetime import datetime

from src.collectors.base import CollectedItem, normalize_doi
from src.http.client import get_shared_client
from src.storage.db import AsyncSessionLocal
from src.storage.models import CrawledItem
from sqlalchemy import select, update

logger = logging.getLogger(__name__)


async def collect(
    config: dict,
    cursor: str | None,
    limit: int,
) -> tuple[list[CollectedItem], str | None]:
    """Backfill stubs created by link_harvester with real metadata.

    config keys: (none required)
    cursor: last processed stub id (as string) or None
    """
    last_id: int | None = int(cursor) if cursor else None

    async with AsyncSessionLocal() as session:
        stmt = (
            select(CrawledItem.id, CrawledItem.url, CrawledItem.external_id,
                   CrawledItem.doi, CrawledItem.raw_payload)
            .where(CrawledItem.source == "link_harvester")
            .where(CrawledItem.description.is_(None))  # not yet backfilled
            .order_by(CrawledItem.id)
            .limit(limit)
        )
        if last_id:
            stmt = stmt.where(CrawledItem.id > last_id)

        rows = (await session.execute(stmt)).fetchall()

    if not rows:
        return [], None

    new_last_id = rows[-1][0]
    enriched_items: list[CollectedItem] = []
    client = get_shared_client()

    for row_id, url, external_id, doi, raw_payload in rows:
        stub_type = (raw_payload or {}).get("stub_type", "")
        raw_id = (raw_payload or {}).get("raw_id", "")

        item: CollectedItem | None = None

        if stub_type == "doi" and doi:
            item = await _fetch_crossref(doi, url, client)
        elif stub_type == "pmid" and raw_id:
            item = await _fetch_pubmed(raw_id, url, client)
        elif stub_type == "pmc" and raw_id:
            item = await _fetch_europepmc(raw_id, url, client)
        elif stub_type == "arxiv":
            logger.debug("link_harvester_backfill: arXiv backfill deferred for %s", url)
            continue
        else:
            logger.debug("link_harvester_backfill: unknown stub type '%s' for %s", stub_type, url)
            continue

        if item:
            await _update_stub(row_id, item)
            enriched_items.append(item)

    logger.info("link_harvester_backfill: enriched %d/%d stubs", len(enriched_items), len(rows))
    next_cursor = str(new_last_id) if len(rows) == limit else None
    return [], next_cursor  # Return empty list; stubs updated in-place via _update_stub


async def _fetch_crossref(doi: str, url: str, client) -> CollectedItem | None:
    """Fetch paper metadata from CrossRef single-record endpoint."""
    try:
        resp = await client.get(f"https://api.crossref.org/works/{doi}")
        data = resp.json().get("message", {})
        title_parts = data.get("title", [])
        title = title_parts[0] if title_parts else doi
        abstract = data.get("abstract", "")
        # Strip JATS XML tags from abstract
        import re
        abstract = re.sub(r"<[^>]+>", "", abstract).strip() or None

        authors = []
        for a in data.get("author", []):
            name = f"{a.get('given', '')} {a.get('family', '')}".strip()
            if name:
                authors.append({"given": a.get("given"), "family": a.get("family")})

        published_parts = (data.get("published-print") or data.get("published-online") or {}).get("date-parts", [[]])
        published_at = None
        if published_parts and published_parts[0]:
            parts = published_parts[0]
            try:
                from datetime import date
                published_at = date(*parts[:3] if len(parts) >= 3 else parts + [1] * (3 - len(parts))).isoformat()
            except Exception:
                pass

        return CollectedItem(
            title=title,
            url=url,
            source="link_harvester_backfill",
            external_id=doi,
            description=abstract,
            content_body=None,
            author=authors[0]["family"] if authors else None,
            authors_json=authors or None,
            published_at=published_at,
            rank_position=None,
            doi=normalize_doi(doi),
            journal=data.get("container-title", [None])[0] if data.get("container-title") else None,
            open_access=data.get("is-referenced-by-count") is not None,  # crude proxy
            engagement={},
            raw_payload={"crossref": True},
        )
    except Exception as exc:
        logger.debug("CrossRef fetch failed for doi=%s: %s", doi, exc)
        return None


async def _fetch_pubmed(pmid: str, url: str, client) -> CollectedItem | None:
    """Fetch paper metadata from PubMed efetch endpoint."""
    try:
        resp = await client.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            params={"db": "pubmed", "id": pmid, "retmode": "xml", "rettype": "abstract"},
        )
        # Parse minimal XML
        from xml.etree import ElementTree as ET
        root = ET.fromstring(resp.text)
        article = root.find(".//Article")
        if article is None:
            return None

        title_el = article.find(".//ArticleTitle")
        title = title_el.text if title_el is not None and title_el.text else f"PMID:{pmid}"

        abstract_texts = [el.text or "" for el in article.findall(".//AbstractText")]
        description = " ".join(abstract_texts).strip() or None

        author_els = article.findall(".//Author")
        authors = []
        for a in author_els:
            last = (a.findtext("LastName") or "").strip()
            first = (a.findtext("ForeName") or a.findtext("Initials") or "").strip()
            if last:
                authors.append({"family": last, "given": first})

        pub_date = article.find(".//PubDate")
        published_at = None
        if pub_date is not None:
            year = pub_date.findtext("Year")
            month = pub_date.findtext("Month") or "01"
            day = pub_date.findtext("Day") or "01"
            if year:
                try:
                    from dateutil.parser import parse as parse_date
                    published_at = parse_date(f"{year} {month} {day}").date().isoformat()
                except Exception:
                    published_at = year

        journal_el = article.find(".//Journal/Title")
        journal = journal_el.text if journal_el is not None else None

        return CollectedItem(
            title=title,
            url=url,
            source="link_harvester_backfill",
            external_id=f"PMID:{pmid}",
            description=description,
            content_body=None,
            author=authors[0]["family"] if authors else None,
            authors_json=authors or None,
            published_at=published_at,
            rank_position=None,
            doi=None,
            journal=journal,
            open_access=None,
            engagement={},
            raw_payload={"pmid": pmid},
        )
    except Exception as exc:
        logger.debug("PubMed fetch failed for pmid=%s: %s", pmid, exc)
        return None


async def _fetch_europepmc(pmc_id: str, url: str, client) -> CollectedItem | None:
    """Fetch paper metadata from EuropePMC by PMC ID."""
    try:
        resp = await client.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": f"PMC{pmc_id}", "format": "json", "resultType": "core", "pageSize": "1"},
        )
        results = resp.json().get("resultList", {}).get("result", [])
        if not results:
            return None
        r = results[0]
        title = r.get("title", f"PMC{pmc_id}").rstrip(".")
        description = r.get("abstractText") or r.get("abstract")
        return CollectedItem(
            title=title,
            url=url,
            source="link_harvester_backfill",
            external_id=f"PMC{pmc_id}",
            description=description,
            content_body=r.get("fullText") or None,
            author=None,
            authors_json=None,
            published_at=r.get("firstPublicationDate"),
            rank_position=None,
            doi=normalize_doi(r.get("doi")),
            journal=r.get("journalTitle"),
            open_access=r.get("isOpenAccess") == "Y",
            engagement={},
            raw_payload={"europepmc": True},
        )
    except Exception as exc:
        logger.debug("EuropePMC fetch failed for pmc=%s: %s", pmc_id, exc)
        return None


def _parse_date(raw_date: str | None) -> datetime | None:
    """Parse an ISO8601 or RFC-2822 date string into a datetime, or return None."""
    if not raw_date:
        return None
    try:
        from dateutil.parser import parse as _parse
        return _parse(raw_date)
    except Exception:
        return None


async def _update_stub(row_id: int, item: CollectedItem) -> None:
    """Update an existing stub row with real metadata."""
    try:
        async with AsyncSessionLocal() as upd_session:
            await upd_session.execute(
                update(CrawledItem)
                .where(CrawledItem.id == row_id)
                .values(
                    title=item["title"],
                    source=item["source"],
                    description=item.get("description"),
                    content_body=item.get("content_body"),
                    author=item.get("author"),
                    authors_json=item.get("authors_json"),
                    published_at=_parse_date(item.get("published_at")),
                    doi=item.get("doi"),
                    journal=item.get("journal"),
                    open_access=item.get("open_access"),
                )
            )
            await upd_session.commit()
    except Exception as exc:
        logger.warning("Failed to update stub id=%d: %s", row_id, exc)
