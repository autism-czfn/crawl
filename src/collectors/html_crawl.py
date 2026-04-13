"""HTML scraper collector for Tier 2 sites.

Extraction priority (Strategy C from O1):
  1. JSON-LD  (<script type="application/ld+json">)
  2. Open Graph (<meta property="og:*">)
  3. Per-site CSS selectors
  4. If all fail → log warning, return empty, do not crash
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from src.collectors.base import CollectedItem
from src.http.client import get_shared_client
from src.http.human import HumanBehaviorSimulator

logger = logging.getLogger(__name__)

# Per-site CSS selectors: title / body / author / date
_SITE_SELECTORS: dict[str, dict[str, str]] = {
    "autism-society.org": {
        "title": "h1.entry-title",
        "body": "div.entry-content",
        "author": "span.author",
        "date": "time[datetime]",
    },
    "autismsciencefoundation.org": {
        "title": "h1",
        "body": "div.post-content",
        "author": "",
        "date": "time",
    },
    "autismspectrumnews.org": {
        "title": "h1",
        "body": "div.entry-content, article",
        "author": "",
        "date": "time[datetime], .date",
    },
    "frontiersin.org": {
        "title": "h1.JournalFullTitle",
        "body": "div.JournalAbstract",
        "author": "span.author-name",
        "date": "span.article-header-date",
    },
    "aacap.org": {
        "title": "h1, h2.page-title, .PageTitle",
        "body": "#TextContent, .content-area, #mainContent",
        "author": "",
        "date": "",
    },
}

_simulator = HumanBehaviorSimulator()


async def collect(
    config: dict,
    cursor: str | None,
    limit: int,
) -> tuple[list[CollectedItem], str | None]:
    """
    config keys:
      base_url: str   — listing page URL to crawl for article links
    cursor: URL of last article processed (skip older) or None
    """
    base_url: str = config["base_url"]
    client = get_shared_client()
    domain = urlparse(base_url).netloc.lstrip("www.")

    # Simulate human behavior before fetching
    await _simulator.pre_request_delay()
    await _simulator.maybe_visit_homepage(client, base_url)
    await _simulator.maybe_prefetch_favicon(client, base_url)

    # Fetch listing page
    try:
        resp = await client.get(
            base_url,
            use_browser_ua=True,
            check_robots=True,
            headers={"Referer": f"https://{urlparse(base_url).netloc}/"},
        )
    except PermissionError as exc:
        logger.warning("html_crawl blocked: %s", exc)
        return [], cursor
    except Exception as exc:
        logger.error("html_crawl listing fetch failed for %s: %s", base_url, exc)
        return [], cursor

    soup = BeautifulSoup(resp.text, "html.parser")
    article_urls = _extract_article_links(soup, base_url)

    if not article_urls:
        logger.warning("html_crawl: no article links found on %s", base_url)
        return [], cursor

    # Skip URLs already seen (cursor = last processed URL)
    if cursor and cursor in article_urls:
        idx = article_urls.index(cursor)
        article_urls = article_urls[:idx]

    items: list[CollectedItem] = []
    new_cursor: str | None = article_urls[0] if article_urls else cursor

    for url in article_urls[:limit]:
        await asyncio.sleep(0.5)  # Strategy 9: between-request delay
        await _simulator.pre_request_delay()

        try:
            art_resp = await client.get(
                url,
                use_browser_ua=True,
                headers={"Referer": base_url},
            )
            _simulator.record_real_request()
        except PermissionError as exc:
            _simulator.on_blocked()
            logger.warning("html_crawl article blocked: %s", exc)
            continue
        except Exception as exc:
            logger.warning("html_crawl article fetch failed %s: %s", url, exc)
            continue

        art_soup = BeautifulSoup(art_resp.text, "html.parser")
        item = _extract_article(art_soup, url, domain, base_url)
        if item:
            items.append(item)

    return items, new_cursor


def _extract_article_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Find article links on a listing page."""
    base_domain = urlparse(base_url).netloc

    # Look for common article link patterns
    candidates: list[str] = []

    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        # Only same-domain links with meaningful paths
        if parsed.netloc == base_domain and len(parsed.path) > 1:
            # Exclude obvious non-article paths
            path = parsed.path.lower()
            if any(x in path for x in ("/tag/", "/category/", "/author/", "/feed/", "/page/", "#")):
                continue
            if full_url not in candidates:
                candidates.append(full_url)

    return candidates


def _extract_article(
    soup: BeautifulSoup,
    url: str,
    domain: str,
    base_url: str,
) -> CollectedItem | None:
    """Extract article metadata using Strategy C priority order."""

    # 1. Try JSON-LD
    item = _from_jsonld(soup, url, domain)
    if item:
        return item

    # 2. Try Open Graph
    item = _from_opengraph(soup, url, domain)
    if item:
        return item

    # 3. Try per-site CSS selectors
    item = _from_css_selectors(soup, url, domain)
    if item:
        return item

    logger.warning("html_crawl: all extraction strategies failed for %s", url)
    return None


def _from_jsonld(soup: BeautifulSoup, url: str, domain: str) -> CollectedItem | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            # Handle array
            if isinstance(data, list):
                data = next((d for d in data if d.get("@type") in ("Article", "NewsArticle", "BlogPosting")), data[0] if data else {})

            title = data.get("headline") or data.get("name", "")
            if not title:
                continue

            description = data.get("description") or data.get("abstract")
            published_at = data.get("datePublished")
            author_data = data.get("author")
            author: str | None = None
            if isinstance(author_data, dict):
                author = author_data.get("name")
            elif isinstance(author_data, list) and author_data:
                author = author_data[0].get("name")
            elif isinstance(author_data, str):
                author = author_data

            return CollectedItem(
                title=title.strip(),
                url=url,
                source="html_crawl",
                external_id=None,
                description=_clean_text(description),
                content_body=None,
                author=author,
                authors_json=None,
                published_at=published_at,
                rank_position=None,
                doi=None,
                journal=domain,
                open_access=None,
                engagement={},
                raw_payload={"jsonld": data},
            )
        except (json.JSONDecodeError, StopIteration, KeyError):
            continue
    return None


def _from_opengraph(soup: BeautifulSoup, url: str, domain: str) -> CollectedItem | None:
    def og(prop: str) -> str | None:
        tag = soup.find("meta", property=f"og:{prop}")
        return tag["content"].strip() if tag and tag.get("content") else None

    def meta_name(name: str) -> str | None:
        tag = soup.find("meta", attrs={"name": name})
        return tag["content"].strip() if tag and tag.get("content") else None

    title = og("title") or meta_name("title")
    if not title:
        return None

    description = og("description") or meta_name("description")
    published_at = meta_name("article:published_time") or og("updated_time")
    author = meta_name("author") or meta_name("article:author")

    return CollectedItem(
        title=title,
        url=url,
        source="html_crawl",
        external_id=None,
        description=_clean_text(description),
        content_body=None,
        author=author,
        authors_json=None,
        published_at=published_at,
        rank_position=None,
        doi=None,
        journal=domain,
        open_access=None,
        engagement={},
        raw_payload={"og:title": title},
    )


def _from_css_selectors(soup: BeautifulSoup, url: str, domain: str) -> CollectedItem | None:
    selectors = _SITE_SELECTORS.get(domain)
    if not selectors:
        return None

    def sel(css: str) -> str | None:
        if not css:
            return None
        for s in css.split(","):
            el = soup.select_one(s.strip())
            if el:
                text = el.get("datetime") or el.get_text(" ", strip=True)
                return text.strip() or None
        return None

    title = sel(selectors.get("title", ""))
    if not title:
        return None

    return CollectedItem(
        title=title,
        url=url,
        source="html_crawl",
        external_id=None,
        description=_clean_text(sel(selectors.get("body", ""))),
        content_body=None,
        author=sel(selectors.get("author", "")),
        authors_json=None,
        published_at=sel(selectors.get("date", "")),
        rank_position=None,
        doi=None,
        journal=domain,
        open_access=None,
        engagement={},
        raw_payload={"css_selector": True},
    )


def _clean_text(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r"<[^>]+>", "", text)  # strip HTML
    text = re.sub(r"\s+", " ", text).strip()
    return text or None
