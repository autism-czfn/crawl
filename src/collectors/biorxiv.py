"""bioRxiv/medRxiv preprint collector (date-range API, client-side keyword filter).

Full-text strategy:
  All bioRxiv/medRxiv preprints are open access.  For each paper, fetch the
  full HTML from biorxiv.org/medrxiv.org and extract text with trafilatura.
  Falls back to PDF if HTML extraction fails.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from src.collectors.base import CollectedItem, normalize_doi
from src.collectors.fulltext import fetch_biorxiv_fulltext
from src.http.client import get_shared_client

logger = logging.getLogger(__name__)

_BASE = "https://api.biorxiv.org/details/{server}/{start}/{end}/{cursor}/json"


async def collect(
    config: dict,
    cursor: str | None,
    limit: int,
) -> tuple[list[CollectedItem], str | None]:
    """
    config keys:
      query:  str   — keyword filter (applied client-side to title/abstract)
      server: str   — 'biorxiv' or 'medrxiv'
    cursor: 'YYYY-MM-DD/{offset}' or None
    """
    query = config.get("query", "autism").lower()
    server = config.get("server", "medrxiv")
    client = get_shared_client()

    # Parse cursor: 'start_date/offset'
    if cursor:
        try:
            start_str, offset_str = cursor.rsplit("/", 1)
            offset = int(offset_str)
        except ValueError:
            start_str = cursor
            offset = 0
    else:
        # Default: last 30 days
        start_str = (datetime.now(tz=timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        offset = 0

    end_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    url = _BASE.format(server=server, start=start_str, end=end_str, cursor=offset)

    try:
        resp = await client.get(url)
        data = resp.json()
    except Exception as exc:
        logger.error("bioRxiv fetch failed: %s", exc)
        return [], cursor

    collection = data.get("collection", [])
    messages = data.get("messages", [{}])
    total = int(messages[0].get("total", 0)) if messages else 0

    items: list[CollectedItem] = []
    for p in collection:
        title = (p.get("title") or "").strip()
        abstract = (p.get("abstract") or "").strip()

        # Client-side keyword filter
        if query not in title.lower() and query not in abstract.lower():
            continue

        doi = normalize_doi(p.get("doi"))
        if not doi:
            continue
        post_url = f"https://doi.org/{doi}"

        published_at: str | None = None
        date_str = p.get("date") or p.get("published")
        if date_str:
            published_at = f"{date_str}T00:00:00+00:00"

        author = p.get("authors", "").split(";")[0].strip().split(",")[0].strip() or None

        items.append(
            CollectedItem(
                title=title,
                url=post_url,
                source="biorxiv",
                external_id=doi,
                description=abstract or None,
                content_body=None,  # enriched below
                author=author,
                authors_json=None,
                published_at=published_at,
                rank_position=None,
                doi=doi,
                journal=server,
                open_access=True,
                engagement={},
                raw_payload=p,
            )
        )
        if len(items) >= limit:
            break

    # Fetch full text concurrently — all preprints are open access
    client = get_shared_client()

    async def _enrich(item: CollectedItem) -> CollectedItem:
        doi = item.get("doi") or item.get("external_id")
        if doi:
            text = await fetch_biorxiv_fulltext(client, doi, server=server)
            if text:
                item["content_body"] = text
        return item

    items = list(await asyncio.gather(*[_enrich(item) for item in items]))

    full_count = sum(1 for it in items if it.get("content_body"))
    logger.info("bioRxiv: %d papers, %d with full text", len(items), full_count)

    new_offset = offset + len(collection)
    if len(collection) > 0 and new_offset < total:
        next_cursor = f"{start_str}/{new_offset}"
    else:
        next_cursor = None

    return items, next_cursor
