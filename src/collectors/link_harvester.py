"""Link Harvester: scans crawled article content_body for academic paper references.

Runs as a post-processor on already-crawled items. Detects DOI / PubMed / arXiv /
PMC IDs embedded as plain text in content_body, inserts CollectedItem stubs for
new discoveries, and updates last_harvested_at on processed source items.

Stubs are backfilled by link_harvester_backfill collector (separate surface).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from src.collectors.base import CollectedItem
from src.storage.db import AsyncSessionLocal
from src.storage.models import CrawledItem

logger = logging.getLogger(__name__)

# Regex patterns for academic URL/ID detection in plain text
_PATTERNS = [
    # DOI: doi.org/10.xxxx/...
    ("doi", re.compile(r"doi\.org/(10\.[^\s\)\"\>\]]+)")),
    # PubMed: pubmed.ncbi.nlm.nih.gov/DIGITS
    ("pmid", re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d{4,})")),
    # arXiv: arxiv.org/abs/ID
    ("arxiv", re.compile(r"arxiv\.org/abs/([\w\.]+)")),
    # PMC: pmc.ncbi.nlm.nih.gov/PMCdigits
    ("pmc", re.compile(r"pmc\.ncbi\.nlm\.nih\.gov/PMC(\d+)")),
]

_HARVEST_INTERVAL_DAYS = 7
_BATCH_SIZE = 100


async def collect(
    config: dict,
    cursor: str | None,
    limit: int,
) -> tuple[list[CollectedItem], str | None]:
    """Scan crawled items and extract academic paper stubs.

    config keys: (none required)
    cursor: last processed item id (as string) or None
    """
    stubs_created = 0
    last_id: int | None = int(cursor) if cursor else None

    async with AsyncSessionLocal() as session:
        # Query items that need harvesting
        stmt = (
            select(CrawledItem.id, CrawledItem.content_body)
            .where(CrawledItem.content_body.isnot(None))
            .where(
                (CrawledItem.last_harvested_at.is_(None)) |
                (CrawledItem.last_harvested_at <
                 datetime.now(tz=timezone.utc) - timedelta(days=_HARVEST_INTERVAL_DAYS))
            )
            .order_by(CrawledItem.id)
            .limit(_BATCH_SIZE)
        )
        if last_id:
            stmt = stmt.where(CrawledItem.id > last_id)

        rows = (await session.execute(stmt)).fetchall()

    if not rows:
        return [], None

    new_last_id = rows[-1][0]
    all_stubs: list[CollectedItem] = []

    for item_id, content_body in rows:
        if not content_body:
            continue
        stubs = _extract_paper_stubs(content_body)
        if not stubs:
            _update_harvested_at(item_id)
            continue

        # Dedup against DB
        new_stubs = await _filter_known_stubs(stubs)
        all_stubs.extend(new_stubs)
        stubs_created += len(new_stubs)

        await _mark_harvested(item_id)

    logger.info("link_harvester: scanned %d items, created %d stubs", len(rows), stubs_created)
    next_cursor = str(new_last_id) if len(rows) == _BATCH_SIZE else None
    return all_stubs, next_cursor


def _extract_paper_stubs(text: str) -> list[CollectedItem]:
    """Extract CollectedItem stubs from plain text."""
    stubs: list[CollectedItem] = []
    seen_ids: set[str] = set()

    for id_type, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            raw_id = match.group(1).rstrip(".,;:)")
            if id_type == "doi":
                canonical_url = f"https://doi.org/{raw_id}"
                title_placeholder = raw_id
                stub_id = f"doi:{raw_id}"
            elif id_type == "pmid":
                canonical_url = f"https://pubmed.ncbi.nlm.nih.gov/{raw_id}/"
                title_placeholder = f"PMID:{raw_id}"
                stub_id = f"pmid:{raw_id}"
            elif id_type == "arxiv":
                canonical_url = f"https://arxiv.org/abs/{raw_id}"
                title_placeholder = f"arXiv:{raw_id}"
                stub_id = f"arxiv:{raw_id}"
            elif id_type == "pmc":
                canonical_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{raw_id}/"
                title_placeholder = f"PMC{raw_id}"
                stub_id = f"pmc:{raw_id}"
            else:
                continue

            if stub_id in seen_ids:
                continue
            seen_ids.add(stub_id)

            stubs.append(CollectedItem(
                title=title_placeholder,
                url=canonical_url,
                source="link_harvester",
                external_id=stub_id,
                description=None,
                content_body=None,
                author=None,
                authors_json=None,
                published_at=None,
                rank_position=None,
                doi=raw_id if id_type == "doi" else None,
                journal=None,
                open_access=None,
                engagement={},
                raw_payload={"stub_type": id_type, "raw_id": raw_id},
            ))

    return stubs


async def _filter_known_stubs(stubs: list[CollectedItem]) -> list[CollectedItem]:
    """Remove stubs whose URL or DOI already exists in the DB."""
    if not stubs:
        return []

    urls = [s["url"] for s in stubs]
    dois = [s.get("doi") for s in stubs if s.get("doi")]

    async with AsyncSessionLocal() as session:
        url_result = await session.execute(
            select(CrawledItem.url).where(CrawledItem.url.in_(urls))
        )
        known_urls = {row[0] for row in url_result.fetchall()}

        known_dois: set[str] = set()
        if dois:
            doi_result = await session.execute(
                select(CrawledItem.doi).where(CrawledItem.doi.in_(dois))
            )
            known_dois = {row[0] for row in doi_result.fetchall() if row[0]}

    return [
        s for s in stubs
        if s["url"] not in known_urls and (not s.get("doi") or s.get("doi") not in known_dois)
    ]


async def _mark_harvested(item_id: int) -> None:
    """Update last_harvested_at for a processed source item."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(CrawledItem)
            .where(CrawledItem.id == item_id)
            .values(last_harvested_at=datetime.now(tz=timezone.utc))
        )
        await session.commit()


def _update_harvested_at(item_id: int) -> None:
    """Schedule last_harvested_at update (fire-and-forget)."""
    asyncio.create_task(_mark_harvested(item_id))
