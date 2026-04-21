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


def _extract_body(soup: BeautifulSoup) -> str | None:
    """Extract main article body text, stripping nav/footer/sidebar."""
    # Work on a copy to avoid mutating the original
    work = BeautifulSoup(str(soup), "html.parser")
    for tag in work.find_all(["nav", "footer", "aside", "header", "script", "style", "noscript"]):
        tag.decompose()

    # Try common article body selectors in priority order
    selectors = [
        "article",
        "[role='main']",
        "main",
        ".entry-content",
        ".post-content",
        ".article-body",
        ".content-body",
        "#content",
        "#main-content",
    ]
    for sel in selectors:
        el = work.select_one(sel)
        if el:
            text = el.get_text(" ", strip=True)
            if len(text) > 100:  # meaningful content
                return _clean_text(text)

    # Fallback: get body text
    body = work.find("body")
    if body:
        text = body.get_text(" ", strip=True)
        if len(text) > 100:
            return _clean_text(text)
    return None

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
      base_url: str   — listing page URL to crawl for article links
    cursor: URL of last article processed (skip older) or None
    """
    base_url: str = config["base_url"]
    allowed_paths: list[str] = config.get("allowed_paths", [])
    excluded_paths: list[str] = config.get("excluded_paths", [])
    max_crawl_depth: int = config.get("max_crawl_depth", 2)
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
    logger.debug("html_crawl: max_crawl_depth=%d (single-level crawl, depth limited by max_items)", max_crawl_depth)
    article_urls = _extract_article_links(soup, base_url, allowed_paths, excluded_paths)

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


def _extract_article_links(soup: BeautifulSoup, base_url: str, allowed_paths: list[str] = None, excluded_paths: list[str] = None) -> list[str]:
    """Find article links on a listing page."""
    base_domain = urlparse(base_url).netloc

    # Look for common article link patterns
    candidates: list[str] = []

    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        full_url = urljoin(base_url, href)

        # Enforce same-domain, blocked-segment, and minimum-depth rules.
        # is_content_url() supersedes the old inline substring checks.
        if not is_content_url(full_url, base_domain):
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
                content_body=_extract_body(soup),
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
        content_body=_extract_body(soup),
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
        content_body=_extract_body(soup),
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
