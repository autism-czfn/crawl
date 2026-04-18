"""CDC Data API collector: fetches autism prevalence datasets via SODA REST API.

Useful for prevalence/statistics queries (ADDM network data), not for
general health guidance articles. Supplement to the CDC html_crawl surfaces.

API docs: https://dev.socrata.com/docs/queries/
No API key required (unauthenticated access).

Usage in surfaces.json:
  {
    "key": "cdc_data_autism",
    "platform": "cdc_data",
    "config": {
      "dataset_ids": ["ivnq-vjsf", "8pt5-q6wp"],
      "search_term": "autism"
    }
  }
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from src.collectors.base import CollectedItem
from src.http.client import get_shared_client

logger = logging.getLogger(__name__)

_BASE_URL = "https://data.cdc.gov"

# Known autism-related dataset IDs on data.cdc.gov
_DEFAULT_DATASET_IDS = [
    "ivnq-vjsf",  # ADDM Autism Prevalence (8-year-olds)
    "8pt5-q6wp",  # Developmental Disabilities Monitoring
]


async def collect(
    config: dict,
    cursor: str | None,
    limit: int,
) -> tuple[list[CollectedItem], str | None]:
    """
    config keys:
      dataset_ids:  list[str]  — SODA dataset identifiers (default: ADDM datasets)
      search_term:  str        — filter term (default: "autism")
    cursor: offset for pagination or None
    """
    dataset_ids: list[str] = config.get("dataset_ids", _DEFAULT_DATASET_IDS)
    search_term: str = config.get("search_term", "autism")
    client = get_shared_client()

    offset = int(cursor) if cursor else 0
    items: list[CollectedItem] = []

    for dataset_id in dataset_ids:
        # Fetch dataset metadata
        meta_url = f"{_BASE_URL}/api/views/{dataset_id}.json"
        try:
            meta_resp = await client.get(meta_url)
            meta = meta_resp.json()
        except Exception as exc:
            logger.warning("cdc_data: metadata fetch failed for %s: %s", dataset_id, exc)
            continue

        dataset_name = meta.get("name", dataset_id)
        dataset_desc = meta.get("description", "")
        dataset_url = f"{_BASE_URL}/dataset/{dataset_id}"
        updated_at = meta.get("rowsUpdatedAt")

        # Fetch rows via SODA API
        query_url = (
            f"{_BASE_URL}/resource/{dataset_id}.json"
            f"?$q={quote(search_term)}"
            f"&$limit={limit}"
            f"&$offset={offset}"
        )
        try:
            data_resp = await client.get(query_url)
            rows = data_resp.json()
        except Exception as exc:
            logger.warning("cdc_data: query failed for %s: %s", dataset_id, exc)
            continue

        if not rows:
            # No matching rows — still create an item for the dataset itself
            items.append(CollectedItem(
                title=dataset_name,
                url=dataset_url,
                source="cdc_data",
                external_id=dataset_id,
                description=dataset_desc[:1000] if dataset_desc else None,
                content_body=None,
                author="CDC",
                authors_json=None,
                published_at=None,
                rank_position=None,
                doi=None,
                journal="data.cdc.gov",
                open_access=True,
                engagement={"row_count": meta.get("viewCount", 0)},
                raw_payload={"dataset_id": dataset_id},
            ))
            continue

        # Create one item per dataset with a summary of the data
        summary_lines = []
        for row in rows[:10]:  # Summarize first 10 rows
            # SODA returns flat dicts; pick meaningful fields
            line_parts = [f"{k}: {v}" for k, v in list(row.items())[:5]]
            summary_lines.append(", ".join(line_parts))

        content_body = f"Dataset: {dataset_name}\n\n" + "\n".join(summary_lines)

        items.append(CollectedItem(
            title=dataset_name,
            url=dataset_url,
            source="cdc_data",
            external_id=dataset_id,
            description=dataset_desc[:1000] if dataset_desc else None,
            content_body=content_body,
            author="CDC",
            authors_json=None,
            published_at=None,
            rank_position=None,
            doi=None,
            journal="data.cdc.gov",
            open_access=True,
            engagement={"row_count": len(rows), "view_count": meta.get("viewCount", 0)},
            raw_payload={"dataset_id": dataset_id, "sample_rows": rows[:3]},
        ))

    new_cursor = str(offset + limit) if items else cursor
    return items, new_cursor
