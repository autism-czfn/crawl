"""DOAJ (Directory of Open Access Journals) REST API collector.

Full-text strategy:
  DOAJ only indexes open-access articles.  When the record carries a
  'fulltext' link, fetch it (HTML via trafilatura, or PDF if the link
  points at one) and use it as content_body; falls back to the abstract.
"""
from __future__ import annotations

import asyncio
import logging

from src.collectors.base import CollectedItem, normalize_doi
from src.collectors.fulltext import fetch_html_and_extract, fetch_pdf_url
from src.http.client import get_shared_client

logger = logging.getLogger(__name__)

_BASE = "https://doaj.org/api/search/articles/{query}"


async def collect(
    config: dict,
    cursor: str | None,
    limit: int,
) -> tuple[list[CollectedItem], str | None]:
    """
    config keys:
      query: str
    cursor: page number as string
    """
    query = config.get("query", "autism")
    page = int(cursor or "1")
    client = get_shared_client()

    url = _BASE.format(query=query)
    params = {
        "page": page,
        "pageSize": min(limit, 100),
        "sort": "created_date:desc",
    }

    try:
        resp = await client.get(url, params=params)
        data = resp.json()
    except Exception as exc:
        logger.error("DOAJ fetch failed: %s", exc)
        return [], cursor

    results = data.get("results", [])
    total = data.get("total", 0)

    items: list[CollectedItem] = []
    for r in results:
        bibjson = r.get("bibjson", {})
        title = (bibjson.get("title") or "").strip()
        if not title:
            continue

        doi = normalize_doi(
            next((i.get("id") for i in bibjson.get("identifier", []) if i.get("type") == "doi"), None)
        )

        # Best URL: DOI, then DOAJ link, then first link
        links = bibjson.get("link", [])
        full_text_link = next((l.get("url") for l in links if l.get("type") == "fulltext"), None)
        doaj_url = r.get("id")
        url_val = (f"https://doi.org/{doi}" if doi else None) or full_text_link or (
            f"https://doaj.org/article/{doaj_url}" if doaj_url else ""
        )
        if not url_val:
            continue

        authors_json = []
        for au in bibjson.get("author", []):
            name = au.get("name", "")
            parts = name.rsplit(" ", 1)
            family = parts[-1] if parts else name
            given = parts[0] if len(parts) > 1 else None
            if family:
                authors_json.append({"family": family, "given": given})

        pub_year = bibjson.get("year")
        published_at = f"{pub_year}-01-01T00:00:00+00:00" if pub_year else None

        journal_info = bibjson.get("journal", {})
        journal_name = journal_info.get("title")

        abstract_list = bibjson.get("abstract", "")
        abstract = abstract_list.strip() if isinstance(abstract_list, str) else None

        items.append(
            CollectedItem(
                title=title,
                url=url_val,
                source="doaj",
                external_id=doaj_url,
                description=abstract,
                content_body=None,
                author=authors_json[0]["family"] if authors_json else None,
                authors_json=authors_json or None,
                published_at=published_at,
                rank_position=None,
                doi=doi,
                journal=journal_name,
                open_access=True,
                engagement={},
                raw_payload={**r, "_full_text_link": full_text_link},
            )
        )

    # Fetch full text concurrently — DOAJ articles are all open access
    client = get_shared_client()

    async def _enrich(item: CollectedItem) -> CollectedItem:
        link = item.get("raw_payload", {}).pop("_full_text_link", None)
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
    logger.info("DOAJ: %d articles, %d with full text", len(items), full_count)

    page_size = len(results)
    fetched = (page - 1) * min(limit, 100) + page_size
    next_cursor = str(page + 1) if fetched < total and page_size > 0 else None
    return items, next_cursor
