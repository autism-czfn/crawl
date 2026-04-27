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
import fnmatch
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from src.collectors.base import CollectedItem
from src.collectors.url_filter import is_content_url
from src.http.client import get_shared_client
from src.http.human import HumanBehaviorSimulator

logger = logging.getLogger(__name__)


from src.extractors.html import extract_body as _extract_body_shared

# Per-site CSS selectors: title / body / author / date
_SITE_SELECTORS: dict[str, dict[str, str]] = {
    "autismsociety.org": {
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
    # --- Non-English sites ---
    "has-sante.fr": {
        "title": "h1",
        "body": "div.inner-pages",
        "author": "",
        "date": "",
    },
    "neurologen-und-psychiater-im-netz.org": {
        "title": "title",  # no h1 on these pages; <title> tag is the best source
        "body": "div.main.kpsychcontent, div.ce-bodytext",
        "author": "",
        "date": "",
    },
    "rehab.go.jp": {
        "title": "h1",
        "body": "article, div#primary.content-primary",
        "author": "",
        "date": "",
    },
    "autismo.org.es": {
        "title": "h1.mod_cta_h",
        "body": "div#main, div.container-general",
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
      base_url:            str        — listing page URL to crawl for article links
      allowed_paths:       list[str]  — path glob patterns to allow
      excluded_paths:      list[str]  — path glob patterns to exclude
      max_crawl_depth:     int        — BFS depth (default 1; 1 = listing->articles only)
      follow_child_links:  bool       — follow links found on article pages (default false)
      child_link_domains:  list[str]  — extra trusted domains for cross-domain link detection
      selectors:           dict       — per-surface CSS selectors override
      min_path_segments:   int        — override minimum path depth filter (default 2)
    cursor: URL of last article processed (skip older) or None
    """
    base_url: str = config["base_url"]
    allowed_paths: list[str] = config.get("allowed_paths", [])
    excluded_paths: list[str] = config.get("excluded_paths", [])
    max_crawl_depth: int = config.get("max_crawl_depth", 1)
    follow_child_links: bool = config.get("follow_child_links", False)
    child_link_domains: list[str] = config.get("child_link_domains", [])
    surface_selectors: dict = config.get("selectors", {})
    min_path_segments: int = config.get("min_path_segments", 2)
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
    article_urls = _extract_article_links(
        soup, base_url, allowed_paths, excluded_paths, min_path_segments
    )

    if not article_urls:
        logger.warning("html_crawl: no article links found on %s", base_url)
        return [], cursor

    # Skip URLs already seen (cursor = last processed URL)
    if cursor and cursor in article_urls:
        idx = article_urls.index(cursor)
        article_urls = article_urls[:idx]

    # Pre-flight dedup: filter out URLs already in the DB
    article_urls = await _filter_known_urls(article_urls)

    items: list[CollectedItem] = []
    new_cursor: str | None = article_urls[0] if article_urls else cursor

    # BFS frontier: (url, depth)
    from collections import deque
    frontier: deque[tuple[str, int]] = deque(
        (url, 1) for url in article_urls[:limit]
    )
    visited: set[str] = set()

    while frontier and len(items) < limit:
        url, depth = frontier.popleft()
        if url in visited:
            continue
        visited.add(url)

        await asyncio.sleep(0.5)
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
            logger.warning("html_crawl article blocked at depth %d: %s", depth, exc)
            continue
        except Exception as exc:
            logger.warning("html_crawl article fetch failed %s: %s", url, exc)
            continue

        art_soup = BeautifulSoup(art_resp.text, "html.parser")
        item = _extract_article(art_soup, url, domain, base_url, surface_selectors)
        if item:
            items.append(item)

        # BFS: enqueue child links if depth allows
        if depth < max_crawl_depth or follow_child_links:
            child_links = _extract_article_links(
                art_soup, url, allowed_paths, excluded_paths, min_path_segments
            )
            next_depth = depth + 1
            for child in child_links:
                if child not in visited and len(items) + len(frontier) < limit * 2:
                    frontier.append((child, next_depth))

    return items, new_cursor


def _matches_path_filter(url: str, allowed_paths: list[str], excluded_paths: list[str]) -> bool:
    """Check if URL path matches allowed patterns and doesn't match excluded patterns."""
    path = urlparse(url).path
    # If excluded_paths defined and path matches any, reject
    if excluded_paths:
        for pattern in excluded_paths:
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path.rstrip('/'), pattern):
                return False
    # If allowed_paths defined, path must match at least one
    if allowed_paths:
        return any(
            fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path.rstrip('/'), p)
            for p in allowed_paths
        )
    return True  # No filters = allow all


async def _filter_known_urls(urls: list[str]) -> list[str]:
    """Filter out URLs already present in the DB. Returns only unvisited URLs."""
    if not urls:
        return urls
    from src.storage.db import AsyncSessionLocal
    from src.storage.models import CrawledItem
    from sqlalchemy import select
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(CrawledItem.url).where(CrawledItem.url.in_(urls))
        )
        known = {row[0] for row in result.fetchall()}
    filtered = [u for u in urls if u not in known]
    logger.debug("html_crawl pre-flight dedup: %d urls -> %d new", len(urls), len(filtered))
    return filtered


def _extract_article_links(soup: BeautifulSoup, base_url: str, allowed_paths: list[str] = None, excluded_paths: list[str] = None, min_path_segments: int = 2) -> list[str]:
    """Find article links on a listing page."""
    base_domain = urlparse(base_url).netloc

    # Look for common article link patterns
    candidates: list[str] = []

    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        full_url = urljoin(base_url, href)

        # Enforce same-domain, blocked-segment, and minimum-depth rules.
        # is_content_url() supersedes the old inline substring checks.
        if not is_content_url(full_url, base_domain, min_path_segments=min_path_segments):
            continue

        if allowed_paths or excluded_paths:
            if not _matches_path_filter(full_url, allowed_paths or [], excluded_paths or []):
                continue

        if full_url not in candidates:
            candidates.append(full_url)

    return candidates


def _extract_article(
    soup: BeautifulSoup,
    url: str,
    domain: str,
    base_url: str,
    surface_selectors: dict | None = None,
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
    item = _from_css_selectors(soup, url, domain, surface_selectors)
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
                content_body=_extract_body_shared(soup),
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
        content_body=_extract_body_shared(soup),
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


def _from_css_selectors(soup: BeautifulSoup, url: str, domain: str, surface_selectors: dict | None = None) -> CollectedItem | None:
    selectors = surface_selectors or _SITE_SELECTORS.get(domain)
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
        content_body=_extract_body_shared(soup),
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
