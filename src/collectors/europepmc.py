"""Europe PMC REST API collector.

Full-text strategy:
  For open-access articles, call the EuropePMC full-text XML API.
  Falls back to the abstract if full text is unavailable.
"""
from __future__ import annotations

import asyncio
import logging

from src.collectors.base import CollectedItem, normalize_doi
from src.collectors.fulltext import fetch_europepmc_fulltext
from src.http.client import get_shared_client

logger = logging.getLogger(__name__)

_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


async def collect(
    config: dict,
    cursor: str | None,
    limit: int,
) -> tuple[list[CollectedItem], str | None]:
    """
    config keys:
      query: str
    cursor: cursorMark string (None → '*')
    """
    query = config.get("query", "autism OR ASD OR autistic")
    cursor_mark = cursor or "*"
    client = get_shared_client()

    params = {
        "query": query,
        "resultType": "core",
        "pageSize": limit,
        "cursorMark": cursor_mark,
        "format": "json",
        "sort": "P_PDATE_D desc",
    }

    try:
        resp = await client.get(_BASE, params=params)
        data = resp.json()
    except Exception as exc:
        logger.error("EuropePMC fetch failed: %s", exc)
        return [], cursor

    results = data.get("resultList", {}).get("result", [])
    next_cursor = data.get("nextCursorMark")

    items: list[CollectedItem] = []
    for r in results:
        title = (r.get("title") or "").strip()
        if not title:
            continue

        pmid = r.get("pmid")
        pmcid = r.get("pmcid")
        doi = normalize_doi(r.get("doi"))

        if pmid:
            url = f"https://europepmc.org/article/MED/{pmid}"
        elif pmcid:
            url = f"https://europepmc.org/article/PMC/{pmcid}"
        elif doi:
            url = f"https://doi.org/{doi}"
        else:
            url = r.get("fullTextUrlList", {}).get("fullTextUrl", [{}])[0].get("url", "")
        if not url:
            continue

        # Authors
        authors_json = []
        for au in r.get("authorList", {}).get("author", []):
            family = au.get("lastName") or au.get("collectiveName")
            given = au.get("firstName")
            if family:
                authors_json.append({"family": family, "given": given})

        published_at: str | None = None
        pub_date = r.get("firstPublicationDate") or r.get("pubYear")
        if pub_date:
            if len(pub_date) == 4:
                published_at = f"{pub_date}-01-01T00:00:00+00:00"
            else:
                published_at = f"{pub_date}T00:00:00+00:00"

        is_oa = r.get("isOpenAccess", "N") == "Y"

        # Determine source type and ID for full-text API
        epmc_source = r.get("source", "MED")  # MED, PMC, PPR, etc.
        epmc_id = pmid or pmcid or r.get("id")

        items.append(
            CollectedItem(
                title=title,
                url=url,
                source="europepmc",
                external_id=pmid or pmcid,
                description=r.get("abstractText") or None,
                content_body=r.get("fullText") or None,  # enriched below
                author=authors_json[0]["family"] if authors_json else None,
                authors_json=authors_json or None,
                published_at=published_at,
                rank_position=None,
                doi=doi,
                journal=r.get("journalTitle"),
                open_access=is_oa,
                engagement={},
                raw_payload={**r, "_epmc_source": epmc_source, "_epmc_id": epmc_id, "_is_oa": is_oa},
            )
        )

    # Fetch full text concurrently for open-access articles that need it
    client = get_shared_client()

    async def _enrich(item: CollectedItem) -> CollectedItem:
        payload = item.get("raw_payload", {})
        if not payload.get("_is_oa"):
            return item
        # Already have good full text from the API response
        if item.get("content_body") and len(item["content_body"]) >= 300:
            return item
        epmc_src = payload.get("_epmc_source", "MED")
        epmc_id = payload.get("_epmc_id")
        if not epmc_id:
            return item
        text = await fetch_europepmc_fulltext(client, epmc_src, epmc_id)
        if text:
            item["content_body"] = text
        return item

    items = list(await asyncio.gather(*[_enrich(item) for item in items]))

    # Clean up internal keys from raw_payload
    for item in items:
        for k in ("_epmc_source", "_epmc_id", "_is_oa"):
            item.get("raw_payload", {}).pop(k, None)

    oa_count = sum(1 for it in items if it.get("content_body"))
    logger.info("EuropePMC: %d articles, %d with full text", len(items), oa_count)

    return items, next_cursor if next_cursor and next_cursor != cursor_mark else None
