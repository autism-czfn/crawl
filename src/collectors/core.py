"""CORE API v3 collector (full-text open access papers)."""
from __future__ import annotations

import logging

from src.collectors.base import CollectedItem, normalize_doi
from src.config import settings
from src.http.client import get_shared_client

logger = logging.getLogger(__name__)

_BASE = "https://api.core.ac.uk/v3/search/works"


async def collect(
    config: dict,
    cursor: str | None,
    limit: int,
) -> tuple[list[CollectedItem], str | None]:
    """
    config keys:
      query: str
    cursor: offset as string
    """
    query = config.get("query", "autism spectrum disorder")
    offset = int(cursor or "0")
    client = get_shared_client()

    params: dict = {
        "q": query,
        "limit": min(limit, 100),
        "offset": offset,
    }

    headers: dict = {}
    if settings.CORE_API_KEY:
        headers["Authorization"] = f"Bearer {settings.CORE_API_KEY}"

    try:
        resp = await client.get(_BASE, params=params, headers=headers)
        data = resp.json()
    except Exception as exc:
        logger.error("CORE fetch failed: %s", exc)
        return [], cursor

    results = data.get("results", [])
    total = data.get("totalHits", 0)
    new_offset = offset + len(results)

    items: list[CollectedItem] = []
    for r in results:
        title = (r.get("title") or "").strip()
        if not title:
            continue

        doi = normalize_doi(r.get("doi"))
        url = (
            r.get("downloadUrl")
            or r.get("sourceFulltextUrls", [None])[0]
            or (f"https://doi.org/{doi}" if doi else "")
            or f"https://core.ac.uk/works/{r.get('id', '')}"
        )
        if not url:
            continue

        authors_json = []
        for au in r.get("authors", []):
            name = au.get("name", "")
            parts = name.rsplit(" ", 1)
            family = parts[-1] if parts else name
            given = parts[0] if len(parts) > 1 else None
            if family:
                authors_json.append({"family": family, "given": given})

        pub_date = r.get("publishedDate") or r.get("yearPublished")
        published_at: str | None = None
        if pub_date:
            if len(str(pub_date)) == 4:
                published_at = f"{pub_date}-01-01T00:00:00+00:00"
            else:
                published_at = f"{pub_date}T00:00:00+00:00" if "T" not in str(pub_date) else str(pub_date)

        # CORE's fullText field sometimes contains only the title (~35 chars).
        # Require at least 200 characters of actual content before storing.
        raw_full_text = (r.get("fullText") or "").strip()
        content_body = raw_full_text if len(raw_full_text) >= 200 else None

        items.append(
            CollectedItem(
                title=title,
                url=url,
                source="core",
                external_id=str(r.get("id", "")),
                description=r.get("abstract") or None,
                content_body=content_body,
                author=authors_json[0]["family"] if authors_json else None,
                authors_json=authors_json or None,
                published_at=published_at,
                rank_position=None,
                doi=doi,
                journal=r.get("journals", [{}])[0].get("title") if r.get("journals") else None,
                open_access=True,
                engagement={},
                raw_payload=r,
            )
        )

    next_cursor = str(new_offset) if new_offset < total else None
    return items, next_cursor
