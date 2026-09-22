"""ClinicalTrials.gov API v2 collector.

Full-text strategy:
  The registry API itself exposes a DetailedDescription field (the full
  study protocol description), so no external fetch is needed — just
  request the field and store it as content_body alongside the
  BriefSummary abstract.
"""
from __future__ import annotations

import logging

from src.collectors.base import CollectedItem
from src.http.client import get_shared_client

logger = logging.getLogger(__name__)

_BASE = "https://clinicaltrials.gov/api/v2/studies"


async def collect(
    config: dict,
    cursor: str | None,
    limit: int,
) -> tuple[list[CollectedItem], str | None]:
    """
    config keys:
      query:  str   — search term
      status: str   — comma-separated statuses (e.g. 'RECRUITING,ACTIVE_NOT_RECRUITING')
    cursor: nextPageToken or None
    """
    query = config.get("query", "autism")
    status = config.get("status", "RECRUITING,ACTIVE_NOT_RECRUITING")
    client = get_shared_client()

    params: dict = {
        "query.term": query,
        "filter.overallStatus": status,
        "pageSize": min(limit, 100),
        "format": "json",
        "fields": "NCTId,BriefTitle,OfficialTitle,BriefSummary,DetailedDescription,OverallStatus,StartDate,CompletionDate,Condition,StudyType,Phase,EnrollmentCount,LeadSponsorName",
    }
    if cursor:
        params["pageToken"] = cursor

    try:
        resp = await client.get(_BASE, params=params)
        data = resp.json()
    except Exception as exc:
        logger.error("ClinicalTrials fetch failed: %s", exc)
        return [], cursor

    studies = data.get("studies", [])
    next_token = data.get("nextPageToken")

    items: list[CollectedItem] = []
    for s in studies:
        proto = s.get("protocolSection", {})
        id_mod = proto.get("identificationModule", {})
        desc_mod = proto.get("descriptionModule", {})
        status_mod = proto.get("statusModule", {})
        sponsor_mod = proto.get("sponsorCollaboratorsModule", {})

        nct_id = id_mod.get("nctId", "")
        title = id_mod.get("officialTitle") or id_mod.get("briefTitle") or ""
        title = title.strip()
        if not title:
            continue

        url = f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else ""
        if not url:
            continue

        summary = desc_mod.get("briefSummary", "").strip() or None
        detailed = desc_mod.get("detailedDescription", "").strip() or None
        start_date = status_mod.get("startDateStruct", {}).get("date")
        published_at = f"{start_date}T00:00:00+00:00" if start_date else None

        sponsor = sponsor_mod.get("leadSponsor", {}).get("name")

        items.append(
            CollectedItem(
                title=title,
                url=url,
                source="clinicaltrials",
                external_id=nct_id,
                description=summary,
                content_body=detailed,
                author=sponsor,
                authors_json=None,
                published_at=published_at,
                rank_position=None,
                doi=None,
                journal=None,
                open_access=True,
                engagement={},
                raw_payload=proto,
            )
        )

    return items, next_token
