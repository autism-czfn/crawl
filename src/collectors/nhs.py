"""NHS UK collector: fetches health content via NHS Conditions API or sitemap fallback.

The NHS Content API v2 is a content-delivery API (fetch by slug).
Fallback: NHS sitemap (sitemap-cms-content.xml) if API is unavailable.

Usage in surfaces.json:
  {
    "key": "nhs_autism",
    "platform": "nhs_api",
    "config": {
      "base_url": "https://www.nhs.uk/conditions/autism/"
    }
  }

Topic slugs are config-driven (config["slugs"]), not hardcoded per topic.
Each slug is a full path relative to https://www.nhs.uk/ — NOT assumed to
live under /conditions/, since real nhs.uk content spans multiple
namespaces (/conditions/, /live-well/, /mental-health/, etc). A surface that
sets no "slugs" falls back to _DEFAULT_SLUGS below (the original autism
list, byte-identical to the pre-generalization behavior), so nhs_autism
needs no config change. Adding a new topic (e.g. nhs_sleep, nhs_eating)
is then just a new surfaces.json entry with its own "slugs" list — no
code change required:

  {
    "key": "nhs_sleep",
    "platform": "nhs_api",
    "config": {
      "slugs": ["conditions/insomnia", "conditions/sleepwalking",
                "live-well/sleep-and-tiredness/healthy-sleep-tips-for-children"]
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
from src.extractors.html import extract_body as _extract_body_shared
from src.http.client import get_shared_client

logger = logging.getLogger(__name__)

# Default slugs used when a surface's config sets no "slugs" list — this is
# the original nhs_autism slug set, with the "/conditions/" prefix now baked
# in explicitly (it used to be implicit in the URL-join logic below) so
# behavior is unchanged for the existing nhs_autism surface.
_DEFAULT_SLUGS = [
    # Autism condition pages
    "conditions/autism",
    "conditions/autism/what-is-autism",
    "conditions/autism/signs-of-autism",
    "conditions/autism/getting-diagnosed",
    "conditions/autism/help-and-support",
    "conditions/autism/autism-and-everyday-life",
    "conditions/autism/autism-and-everyday-life/communicating",
    "conditions/autism/autism-and-everyday-life/community-care-and-support",
    "conditions/developmental-delay",
    "conditions/social-care-and-support-guide",
    # Related conditions that parents search for alongside autism
    "conditions/attention-deficit-hyperactivity-disorder-adhd",
    "conditions/sensory-processing-disorder",
    "conditions/learning-disabilities",
    "conditions/stammering",
    "conditions/selective-mutism",
    "conditions/dyspraxia",
]


def _build_urls(config: dict) -> list[str]:
    """Build the list of nhs.uk URLs to fetch — each slug relative to nhs.uk
    root, not assumed to be under /conditions/ (see module docstring). Pulled
    out as a pure function so URL construction is unit-testable without
    hitting the network.
    """
    slugs: list[str] = config.get("slugs", _DEFAULT_SLUGS)
    return [f"https://www.nhs.uk/{slug.strip('/')}/" for slug in slugs]


async def collect(
    config: dict,
    cursor: str | None,
    limit: int,
) -> tuple[list[CollectedItem], str | None]:
    """
    config keys:
      base_url: str        — unused for URL construction (kept for
                              documentation/back-compat); see "slugs" below
      slugs:    list[str]  — paths relative to https://www.nhs.uk/ to fetch,
                              e.g. "conditions/insomnia" or
                              "live-well/sleep-and-tiredness/healthy-sleep-tips-for-children".
                              Defaults to _DEFAULT_SLUGS (the autism set) if
                              not provided, so existing surfaces are unaffected.
    cursor: URL of last processed page or None
    """
    client = get_shared_client()
    urls = _build_urls(config)

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
    """Extract NHS page main content.

    Delegates to the shared extractor (Trafilatura first, then a CSS
    selector fallback list with a proper 300-char/50-word/boilerplate
    quality gate) instead of a bespoke low-bar selector scan, so NHS
    content is held to the same standard as every other source.
    """
    return _extract_body_shared(
        soup,
        custom_selectors=["#maincontent", "main", ".nhsuk-main-wrapper", "article", "[role='main']"],
    )


def _clean_text(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None
