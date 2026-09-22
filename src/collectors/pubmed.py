"""PubMed NCBI E-utilities collector.

Full-text strategy:
  For each article, check if a PMC ID is present in the XML.  If so, fetch
  the full text from pmc.ncbi.nlm.nih.gov (HTML → trafilatura, or efetch XML
  fallback).  Articles without a PMC ID stay at abstract-only.
"""
from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET

from src.collectors.base import CollectedItem, normalize_doi
from src.collectors.fulltext import fetch_pmc_fulltext
from src.config import settings
from src.http.client import get_shared_client

logger = logging.getLogger(__name__)

_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


async def collect(
    config: dict,
    cursor: str | None,
    limit: int,
) -> tuple[list[CollectedItem], str | None]:
    """
    config keys:
      query: str
      sort:  str  (e.g. 'pub_date')
    cursor: retstart offset as string
    """
    query = config.get("query", "autism spectrum disorder")
    sort = config.get("sort", "pub_date")
    retstart = int(cursor or "0")
    client = get_shared_client()

    # Step 1: esearch to get PMIDs
    search_params: dict = {
        "db": "pubmed",
        "term": query,
        "sort": sort,
        "retmax": limit,
        "retstart": retstart,
        "retmode": "json",
        "usehistory": "n",
    }
    if settings.PUBMED_API_KEY:
        search_params["api_key"] = settings.PUBMED_API_KEY

    try:
        resp = await client.get(_ESEARCH, params=search_params)
        search_data = resp.json()
    except Exception as exc:
        logger.error("PubMed esearch failed: %s", exc)
        return [], cursor

    pmids = search_data.get("esearchresult", {}).get("idlist", [])
    if not pmids:
        return [], cursor

    # Step 2: efetch to get full records as XML
    fetch_params: dict = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "rettype": "abstract",
    }
    if settings.PUBMED_API_KEY:
        fetch_params["api_key"] = settings.PUBMED_API_KEY

    try:
        fetch_resp = await client.get(_EFETCH, params=fetch_params)
        root = ET.fromstring(fetch_resp.text)
    except Exception as exc:
        logger.error("PubMed efetch failed: %s", exc)
        return [], cursor

    parsed: list[tuple[CollectedItem, str | None]] = []
    for article in root.findall(".//PubmedArticle"):
        try:
            item, pmcid = _parse_article(article)
            if item:
                parsed.append((item, pmcid))
        except Exception as exc:
            logger.warning("Failed to parse PubMed article: %s", exc)

    # Fetch full text concurrently for articles that have a PMC ID
    client = get_shared_client()

    async def _enrich(item: CollectedItem, pmcid: str | None) -> CollectedItem:
        if pmcid:
            text = await fetch_pmc_fulltext(client, pmcid)
            if text:
                item["content_body"] = text
        return item

    items: list[CollectedItem] = list(
        await asyncio.gather(*[_enrich(item, pmcid) for item, pmcid in parsed])
    )

    pmc_count = sum(1 for _, pmcid in parsed if pmcid)
    logger.info("PubMed: fetched %d articles, %d have PMC full text available", len(items), pmc_count)

    next_cursor = str(retstart + len(pmids)) if len(pmids) == limit else None
    return items, next_cursor


def _parse_article(article: ET.Element) -> tuple[CollectedItem | None, str | None]:
    medline = article.find("MedlineCitation")
    if medline is None:
        return None, None

    pmid_el = medline.find("PMID")
    pmid = pmid_el.text if pmid_el is not None else None

    art = medline.find("Article")
    if art is None:
        return None, None

    title_el = art.find("ArticleTitle")
    title = "".join(title_el.itertext()).strip() if title_el is not None else ""
    if not title:
        return None

    # Abstract
    abstract_parts = art.findall(".//AbstractText")
    description = " ".join("".join(p.itertext()) for p in abstract_parts).strip() or None

    # Authors
    authors_json = []
    author_el = art.find("AuthorList")
    if author_el is not None:
        for au in author_el.findall("Author"):
            family = au.findtext("LastName")
            given = au.findtext("ForeName")
            if family:
                authors_json.append({"family": family, "given": given})
    first_author = authors_json[0]["family"] if authors_json else None

    # Journal
    journal_title = art.findtext("Journal/Title")

    # Published date
    pub_date = medline.find(".//PubDate")
    published_at: str | None = None
    if pub_date is not None:
        year = pub_date.findtext("Year") or pub_date.findtext("MedlineDate", "")[:4]
        month = pub_date.findtext("Month", "01")
        day = pub_date.findtext("Day", "01")
        _month_map = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
                      "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
                      "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12"}
        month = _month_map.get(month, month).zfill(2)
        day = day.zfill(2)
        if year:
            try:
                published_at = f"{year}-{month}-{day}T00:00:00+00:00"
            except Exception:
                pass

    # DOI and PMC ID
    doi: str | None = None
    pmcid: str | None = None
    for id_el in article.findall(".//ArticleId"):
        id_type = id_el.get("IdType")
        if id_type == "doi":
            doi = normalize_doi(id_el.text)
        elif id_type == "pmc" and id_el.text:
            pmcid = id_el.text.strip()

    url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""

    return CollectedItem(
        title=title,
        url=url,
        source="pubmed",
        external_id=pmid,
        description=description,
        content_body=None,
        author=first_author,
        authors_json=authors_json or None,
        published_at=published_at,
        rank_position=None,
        doi=doi,
        journal=journal_title,
        open_access=None,
        engagement={},
        raw_payload={"pmid": pmid},
    ), pmcid
