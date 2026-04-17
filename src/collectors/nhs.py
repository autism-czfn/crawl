"""NHS UK collector: fetches health content via NHS Conditions API or sitemap fallback.

The NHS Content API v2 is a content-delivery API (fetch by slug).
We use it to retrieve specific conditions pages related to autism.
Fallback: NHS sitemap (sitemap-cms-content.xml) if API is unavailable.

Usage in surfaces.json:
  {
    "key": "nhs_autism",
    "platform": "nhs_api",
    "config": {
      "base_url": "https://www.nhs.uk/conditions/autism/"
    }
  }
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from src.collectors.base import CollectedItem
from src.http.client import get_shared_client

logger = logging.getLogger(__name__)

# NHS autism-related condition slugs to fetch
_AUTISM_SLUGS = [
    "autism",
    "autism/what-is-autism",
    "autism/signs-of-autism",
    "autism/getting-diagnosed",
    "autism/help-and-support",
    "autism/autism-and-everyday-life",
    "autism/autism-and-everyday-life/communicating",
    "autism/autism-and-everyday-life/community-care-and-support",
    "developmental-delay",
    "social-care-and-support-guide",
]

# Related conditions that parents search for alongside autism
_RELATED_SLUGS = [
    "attention-deficit-hyperactivity-disorder-adhd",
    "sensory-processing-disorder",
    "learning-disabilities",
    "stammering",
    "selective-mutism",
    "dyspraxia",
]


async def collect(
    config: dict,
    cursor: str | None,
    limit: int,
) -> tuple[list[CollectedItem], str | None]:
    """
    config keys:
      base_url: str — base NHS conditions URL (e.g. https://www.nhs.uk/conditions/autism/)
    cursor: URL of last processed page or None
    """
    base_url: str = config.get("base_url", "https://www.nhs.uk/conditions/autism/")
    client = get_shared_client()

    # Build list of URLs to fetch
    all_slugs = _AUTISM_SLUGS + _RELATED_SLUGS
    urls = [f"https://www.nhs.uk/conditions/{slug}/" for slug in all_slugs]

    # Skip already-processed URLs
    if cursor and cursor in urls:
        idx = urls.index(cursor)
        urls = urls[idx + 1:]

    items: list[CollectedItem] = []
    new_cursor = cursor

    for url in urls[:limit]:
        await asyncio.sleep(0.5)  # Polite delay

        try:
            resp = await client.get(url, use_browser_ua=True)
        except Exception as exc:
            logger.warning("nhs: fetch failed %s: %s", url, exc)
            continue

        if resp.status_code == 404:
            logger.debug("nhs: 404 for %s, skipping", url)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        item = _extract_page(soup, url)
        if item:
            items.append(item)
            new_cursor = url

    return items, new_cursor


def _extract_page(soup: BeautifulSoup, url: str) -> CollectedItem | None:
    """Extract content from an NHS conditions page."""
    # NHS pages have a consistent structure
    title_el = soup.find("h1")
    if not title_el:
        title_el = soup.find("title")
    if not title_el:
        return None

    title = title_el.get_text(strip=True)
    if not title:
        return None

    # Remove " - NHS" suffix from title
    title = re.sub(r'\s*-\s*NHS\s*$', '', title)

    # Extract description from meta
    desc_tag = soup.find("meta", attrs={"name": "description"})
    description = desc_tag["content"].strip() if desc_tag and desc_tag.get("content") else None

    # Extract main content body
    content_body = _extract_body(soup)

    # Extract last reviewed date
    review_el = soup.find("p", class_="nhsuk-body-s nhsuk-u-secondary-text-color nhsuk-u-margin-top-7 nhsuk-u-margin-bottom-0")
    if not review_el:
        review_el = soup.find(string=re.compile(r"Page last reviewed:"))
    published_at = None
    if review_el:
        text = review_el.get_text() if hasattr(review_el, 'get_text') else str(review_el)
        date_match = re.search(r'(\d{1,2}\s+\w+\s+\d{4})', text)
        if date_match:
            published_at = date_match.group(1)

    return CollectedItem(
        title=title,
        url=url,
        source="nhs_api",
        external_id=urlparse(url).path.strip("/"),
        description=_clean_text(description),
        content_body=content_body,
        author="NHS",
        authors_json=None,
        published_at=published_at,
        rank_position=None,
        doi=None,
        journal="nhs.uk",
        open_access=True,
        engagement={},
        raw_payload={},
    )


def _extract_body(soup: BeautifulSoup) -> str | None:
    """Extract NHS page main content."""
    work = BeautifulSoup(str(soup), "html.parser")
    for tag in work.find_all(["nav", "footer", "aside", "header", "script", "style", "noscript"]):
        tag.decompose()

    # NHS uses specific content containers
    for sel in [
        "#maincontent",
        "main",
        ".nhsuk-main-wrapper",
        "article",
        "[role='main']",
    ]:
        el = work.select_one(sel)
        if el:
            text = el.get_text(" ", strip=True)
            if len(text) > 100:
                return _clean_text(text)

    return None


def _clean_text(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None
