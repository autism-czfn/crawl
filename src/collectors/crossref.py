"""CrossRef API collector (polite pool via mailto).

Full-text strategy:
  CrossRef indexes citation metadata for essentially all scholarly
  literature, most of which sits behind a publisher paywall — so full
  text is only attempted when the work itself signals it's open access
  (a Creative Commons license entry in the response).  Prefers a direct
  PDF link when CrossRef supplies one, otherwise fetches the work's own
  URL.  Falls back to the abstract when no CC signal is present or the
  fetch fails.
"""
from __future__ import annotations

import asyncio
import logging

from src.collectors.base import CollectedItem, normalize_doi
from src.collectors.fulltext import fetch_html_and_extract, fetch_pdf_url
from src.config import settings
from src.http.client import get_shared_client

logger = logging.getLogger(__name__)

_BASE = "https://api.crossref.org/works"


async def collect(
    config: dict,
    cursor: str | None,
    limit: int,
) -> tuple[list[CollectedItem], str | None]:
    """
    config keys:
      query:  str   — keyword search
      filter: str   — e.g. 'type:journal-article'
    cursor: offset as string
    """
    query = config.get("query", "autism")
    extra_filter = config.get("filter", "type:journal-article")
    offset = int(cursor or "0")
    client = get_shared_client()

    params: dict = {
        "query": query,
        "rows": min(limit, 100),
        "offset": offset,
        "sort": "published",
        "order": "desc",
        "mailto": settings.CRAWLER_EMAIL,
    }
    if extra_filter:
        params["filter"] = extra_filter

    try:
        resp = await client.get(_BASE, params=params)
        data = resp.json()
    except Exception as exc:
        logger.error("CrossRef fetch failed: %s", exc)
        return [], cursor

    msg = data.get("message", {})
    works = msg.get("items", [])
    total = msg.get("total-results", 0)
    new_offset = offset + len(works)

    items: list[CollectedItem] = []
    for w in works:
        title_list = w.get("title", [])
        title = title_list[0].strip() if title_list else ""
        if not title:
            continue

        doi = normalize_doi(w.get("DOI"))
        url = w.get("URL") or (f"https://doi.org/{doi}" if doi else "")
        if not url:
            continue

        authors_json = []
        for au in w.get("author", []):
            family = au.get("family")
            given = au.get("given")
            if family:
                authors_json.append({"family": family, "given": given})

        # Published date
        published_at: str | None = None
        pub = w.get("published") or w.get("published-print") or w.get("published-online")
        if pub:
            parts = pub.get("date-parts", [[]])[0]
            if parts:
                year = parts[0]
                month = parts[1] if len(parts) > 1 else 1
                day = parts[2] if len(parts) > 2 else 1
                published_at = f"{year}-{month:02d}-{day:02d}T00:00:00+00:00"

        journal_list = w.get("container-title", [])
        journal = journal_list[0] if journal_list else None

        abstract = w.get("abstract", "").strip() or None

        # OA signal: a Creative Commons license entry means the work is
        # very likely free to fetch, unlike most CrossRef-indexed papers.
        license_list = w.get("license", []) or []
        is_cc = any("creativecommons.org" in (lic.get("URL") or "") for lic in license_list)
        pdf_link = next(
            (l.get("URL") for l in (w.get("link", []) or []) if l.get("content-type") == "application/pdf"),
            None,
        )
        fetch_url = (pdf_link or url) if is_cc else None

        items.append(
            CollectedItem(
                title=title,
                url=url,
                source="crossref",
                external_id=doi,
                description=abstract,
                content_body=None,
                author=authors_json[0]["family"] if authors_json else None,
                authors_json=authors_json or None,
                published_at=published_at,
                rank_position=None,
                doi=doi,
                journal=journal,
                open_access=is_cc or None,
                engagement={},
                raw_payload={**w, "_fetch_url": fetch_url},
            )
        )

    # Fetch full text concurrently for works that signal open access
    client = get_shared_client()

    async def _enrich(item: CollectedItem) -> CollectedItem:
        link = item.get("raw_payload", {}).pop("_fetch_url", None)
        if not link:
            return item
        if link.lower().endswith(".pdf"):
            text = await fetch_pdf_url(client, link)
        else:
            text = await fetch_html_and_extract(client, link)
        if text:
            item["content_body"] = text
        return item

    items = list(await asyncio.gather(*[_enrich(item) for item in items]))

    full_count = sum(1 for it in items if it.get("content_body"))
    logger.info("CrossRef: %d works, %d with full text", len(items), full_count)

    next_cursor = str(new_offset) if new_offset < total else None
    return items, next_cursor
