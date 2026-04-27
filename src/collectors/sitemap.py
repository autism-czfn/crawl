"""Sitemap-based collector: fetches URLs from XML sitemaps and extracts content.

Generic and reusable — works with any site that publishes sitemaps.
Used by Mayo Clinic initially, extensible to any sitemap source.

Usage in surfaces.json:
  {
    "key": "mayo_autism",
    "platform": "sitemap",
    "config": {
      "sitemap_url": "https://www.mayoclinic.org/sitemap.xml",
      "filter_path": "/diseases-conditions/autism"
    }
  }
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from src.collectors.base import CollectedItem
from src.extractors.html import extract_body as _extract_body
from src.http.client import get_shared_client

logger = logging.getLogger(__name__)

# Sitemap XML namespace
_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


async def collect(
    config: dict,
    cursor: str | None,
    limit: int,
) -> tuple[list[CollectedItem], str | None]:
    """
    config keys:
      sitemap_url:  str  — URL of the sitemap.xml (or sitemap index)
      filter_path:  str  — path prefix to filter URLs (e.g. "/diseases-conditions/autism")
    cursor: URL of last processed page or None
    """
    sitemap_url: str = config["sitemap_url"]
    filter_path: str = config.get("filter_path", "")
    client = get_shared_client()

    # Fetch and parse sitemap (handles sitemap index recursively)
    try:
        resp = await client.get(sitemap_url, use_browser_ua=True)
    except Exception as exc:
        logger.error("sitemap fetch failed for %s: %s", sitemap_url, exc)
        return [], cursor

    urls = _parse_sitemap(resp.text, filter_path)

    # If result is sub-sitemap URLs (sitemap index), fetch each and collect page URLs
    if urls and urls[0].endswith(".xml"):
        page_urls: list[str] = []
        for sub_url in urls:
            try:
                sub_resp = await client.get(sub_url, use_browser_ua=True)
                page_urls.extend(_parse_sitemap(sub_resp.text, filter_path))
            except Exception as exc:
                logger.warning("sitemap: sub-sitemap fetch failed %s: %s", sub_url, exc)
            await asyncio.sleep(0.5)
        urls = page_urls

    if not urls:
        logger.warning("sitemap: no matching URLs found in %s (filter: %s)", sitemap_url, filter_path)
        return [], cursor

    # Skip already-processed URLs
    if cursor and cursor in urls:
        idx = urls.index(cursor)
        urls = urls[idx + 1:]  # Move forward past cursor

    items: list[CollectedItem] = []
    new_cursor = cursor

    for url in urls[:limit]:
        await asyncio.sleep(1.0)  # Polite delay

        try:
            page_resp = await client.get(url, use_browser_ua=True, check_robots=True)
        except PermissionError:
            logger.warning("sitemap: robots.txt blocked %s", url)
            continue
        except Exception as exc:
            logger.warning("sitemap: page fetch failed %s: %s", url, exc)
            continue

        soup = BeautifulSoup(page_resp.text, "html.parser")
        item = _extract_page(soup, url)
        if item:
            items.append(item)
            new_cursor = url

    return items, new_cursor


def _parse_sitemap(xml_text: str, filter_path: str) -> list[str]:
    """Parse sitemap XML and return filtered URLs.

    Handles both sitemap index files (containing <sitemap> entries)
    and regular sitemaps (containing <url> entries).
    For sitemap indexes, we only extract the sub-sitemap URLs themselves
    — the caller would need to fetch those separately. For MVP,
    we handle the common case of a flat sitemap.
    """
    urls: list[str] = []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        logger.error("sitemap XML parse error: %s", exc)
        return []

    # Check if this is a sitemap index
    sitemaps = root.findall("sm:sitemap/sm:loc", _NS)
    if sitemaps:
        # This is a sitemap index — extract sub-sitemap URLs
        # For now, just return the sub-sitemap URLs that match the filter
        # The caller can iterate on these
        logger.info("sitemap: found sitemap index with %d sub-sitemaps", len(sitemaps))
        # We can't recursively fetch sub-sitemaps in this call,
        # so return the sub-sitemap URLs that might contain our content
        for sm in sitemaps:
            loc = sm.text
            if loc and (not filter_path or filter_path.lstrip("/") in loc):
                urls.append(loc.strip())
        return urls

    # Regular sitemap — extract <url><loc> entries
    for url_el in root.findall("sm:url", _NS):
        loc = url_el.find("sm:loc", _NS)
        if loc is not None and loc.text:
            url = loc.text.strip()
            if not filter_path or filter_path in urlparse(url).path:
                urls.append(url)

    logger.info("sitemap: found %d matching URLs (filter: '%s')", len(urls), filter_path)
    return urls


def _extract_page(soup: BeautifulSoup, url: str) -> CollectedItem | None:
    """Extract article content from a fetched page."""
    domain = urlparse(url).netloc.lstrip("www.")

    # Try JSON-LD first
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            import json
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = next(
                    (d for d in data if d.get("@type") in
                     ("Article", "NewsArticle", "BlogPosting", "MedicalCondition",
                      "MedicalWebPage", "WebPage")),
                    data[0] if data else {},
                )

            title = data.get("headline") or data.get("name", "")
            if not title:
                continue

            return CollectedItem(
                title=title.strip(),
                url=url,
                source="sitemap",
                external_id=None,
                description=_clean_text(data.get("description") or data.get("abstract")),
                content_body=_extract_body(soup),
                author=_extract_author(data.get("author")),
                authors_json=None,
                published_at=data.get("datePublished"),
                rank_position=None,
                doi=None,
                journal=domain,
                open_access=None,
                engagement={},
                raw_payload={"jsonld": data},
            )
        except Exception:
            continue

    # Try Open Graph
    def og(prop: str) -> str | None:
        tag = soup.find("meta", property=f"og:{prop}")
        return tag["content"].strip() if tag and tag.get("content") else None

    def meta(name: str) -> str | None:
        tag = soup.find("meta", attrs={"name": name})
        return tag["content"].strip() if tag and tag.get("content") else None

    title = og("title") or meta("title") or (soup.find("title") and soup.find("title").get_text(strip=True))
    if not title:
        return None

    return CollectedItem(
        title=title,
        url=url,
        source="sitemap",
        external_id=None,
        description=_clean_text(og("description") or meta("description")),
        content_body=_extract_body(soup),
        author=meta("author"),
        authors_json=None,
        published_at=meta("article:published_time"),
        rank_position=None,
        doi=None,
        journal=domain,
        open_access=None,
        engagement={},
        raw_payload={},
    )


def _extract_author(author_data) -> str | None:
    if isinstance(author_data, dict):
        return author_data.get("name")
    elif isinstance(author_data, list) and author_data:
        return author_data[0].get("name") if isinstance(author_data[0], dict) else str(author_data[0])
    elif isinstance(author_data, str):
        return author_data
    return None


def _clean_text(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None
