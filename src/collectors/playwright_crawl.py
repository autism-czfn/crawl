"""Playwright-based collector for Cloudflare-protected and JS-rendered sites.

Falls back gracefully to standard html_crawl extraction logic once the
rendered HTML is obtained.  Uses a **persistent** browser context so
Cloudflare challenge cookies survive across page loads within a single
collector run.

Usage in surfaces.json:
  {
    "key": "insar_resources",
    "platform": "playwright_crawl",
    "poll_interval_sec": 86400,
    "max_items": 15,
    "config": {
      "start_url": "https://www.autism-insar.org/page/Resources",
      "wait_selector": "body",
      "wait_ms": 5000,
      "follow_links": true,
      "link_selector": "a[href]",
      "same_domain_only": true
    }
  }
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

# Maximum pages to visit per run (safety cap)
_MAX_PAGES = 50

# Per-site CSS selectors — same idea as html_crawl but for JS-rendered pages
_SITE_SELECTORS: dict[str, dict[str, str]] = {
    "autism-insar.org": {
        "title": "h1, h2, .page-title, #page-title",
        "body": "#content-main, .main-content, #ctl01_TemplateBody_WebPartManager1_gwpciaboraliloLayout_ciaboraliloLayout, article, main",
        "author": "",
        "date": "time[datetime], .date, .published-date",
    },
}


async def collect(
    config: dict,
    cursor: str | None,
    limit: int,
) -> tuple[list[CollectedItem], str | None]:
    """
    config keys:
      start_url:        str  — page to load (required)
      wait_selector:    str  — CSS selector to wait for before extracting (default "body")
      wait_ms:          int  — extra ms to wait after selector appears (default 5000)
      follow_links:     bool — crawl internal links found on start page (default true)
      link_selector:    str  — CSS selector for links to follow (default "a[href]")
      same_domain_only: bool — only follow links on same domain (default true)
    cursor: URL of last article processed or None
    """
    start_url: str = config["start_url"]
    wait_selector: str = config.get("wait_selector", "body")
    wait_ms: int = config.get("wait_ms", 5000)
    follow_links: bool = config.get("follow_links", True)
    link_selector: str = config.get("link_selector", "a[href]")
    same_domain_only: bool = config.get("same_domain_only", True)
    allowed_paths: list[str] = config.get("allowed_paths", [])
    excluded_paths: list[str] = config.get("excluded_paths", [])
    max_crawl_depth: int = config.get("max_crawl_depth", 2)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error(
            "playwright not installed — run: pip install playwright && playwright install chromium"
        )
        return [], cursor

    items: list[CollectedItem] = []
    new_cursor: str | None = cursor

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            java_script_enabled=True,
        )

        try:
            # --- Fetch start page ---
            start_html = await _fetch_page(context, start_url, wait_selector, wait_ms)
            if not start_html:
                return [], cursor

            soup = BeautifulSoup(start_html, "html.parser")
            domain = urlparse(start_url).netloc.lstrip("www.")

            # Extract item from start page itself
            item = _extract_item(soup, start_url, domain)
            if item:
                items.append(item)
                new_cursor = start_url

            if not follow_links:
                return items, new_cursor or cursor

            # --- Discover and follow links ---
            link_urls = _extract_links(soup, start_url, link_selector, same_domain_only, allowed_paths, excluded_paths)

            # Skip already-processed URLs
            if cursor and cursor in link_urls:
                idx = link_urls.index(cursor)
                link_urls = link_urls[:idx]

            if link_urls:
                new_cursor = link_urls[0]

            for url in link_urls[: min(limit, _MAX_PAGES)]:
                # Polite delay between pages
                await asyncio.sleep(2.0 + (asyncio.get_event_loop().time() % 1.0))

                page_html = await _fetch_page(context, url, "body", wait_ms)
                if not page_html:
                    continue

                page_soup = BeautifulSoup(page_html, "html.parser")
                item = _extract_item(page_soup, url, domain)
                if item:
                    items.append(item)

        finally:
            await context.close()
            await browser.close()

    return items, new_cursor


async def _fetch_page(
    context: "Any",
    url: str,
    wait_selector: str,
    wait_ms: int,
) -> str | None:
    """Open a page in the browser context, wait for content, return rendered HTML."""
    page = await context.new_page()
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if resp is None:
            logger.warning("playwright_crawl: no response for %s", url)
            return None

        # Wait for Cloudflare challenge to resolve + content to render
        try:
            await page.wait_for_selector(wait_selector, timeout=15_000)
        except Exception:
            logger.debug("playwright_crawl: wait_selector '%s' timed out on %s", wait_selector, url)

        # Extra wait for JS-rendered content / Cloudflare interstitial
        await page.wait_for_timeout(wait_ms)

        # Check if we're still on a Cloudflare challenge page
        title = await page.title()
        if "just a moment" in title.lower():
            logger.warning(
                "playwright_crawl: still on Cloudflare challenge for %s — waiting longer",
                url,
            )
            await page.wait_for_timeout(10_000)
            title = await page.title()
            if "just a moment" in title.lower():
                logger.error("playwright_crawl: could not pass Cloudflare challenge for %s", url)
                return None

        return await page.content()

    except Exception as exc:
        logger.error("playwright_crawl: page load failed for %s: %s", url, exc)
        return None
    finally:
        await page.close()


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


def _extract_links(
    soup: BeautifulSoup,
    base_url: str,
    link_selector: str,
    same_domain_only: bool,
    allowed_paths: list[str] = None,
    excluded_paths: list[str] = None,
) -> list[str]:
    """Find unique links on the page matching the selector."""
    base_domain = urlparse(base_url).netloc
    seen: list[str] = []

    for a in soup.select(link_selector):
        href = a.get("href")
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue

        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        # Enforce same-domain (when requested), blocked-segment, and
        # minimum-depth rules via the shared content-URL filter.
        # When same_domain_only is False we still reject non-content
        # segments on the target domain itself.
        check_domain = base_domain if same_domain_only else parsed.netloc
        if not is_content_url(full_url, check_domain):
            continue

        if allowed_paths or excluded_paths:
            if not _matches_path_filter(full_url, allowed_paths or [], excluded_paths or []):
                continue

        # Normalize: strip fragments and trailing slashes for dedup
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        if parsed.query:
            clean += f"?{parsed.query}"

        if clean not in seen and clean != base_url.rstrip("/"):
            seen.append(clean)

    return seen


def _extract_item(
    soup: BeautifulSoup,
    url: str,
    domain: str,
) -> CollectedItem | None:
    """Extract structured data from rendered HTML using priority: JSON-LD > OG > CSS selectors."""

    # 1. JSON-LD
    item = _from_jsonld(soup, url, domain)
    if item:
        return item

    # 2. Open Graph
    item = _from_opengraph(soup, url, domain)
    if item:
        return item

    # 3. Per-site CSS selectors
    item = _from_css_selectors(soup, url, domain)
    if item:
        return item

    # 4. Last resort: just grab the <title> tag
    title_tag = soup.find("title")
    if title_tag:
        title_text = title_tag.get_text(strip=True)
        if title_text and "just a moment" not in title_text.lower():
            return CollectedItem(
                title=title_text,
                url=url,
                source="playwright_crawl",
                external_id=None,
                description=_get_meta_description(soup),
                content_body=_extract_body(soup),
                author=None,
                authors_json=None,
                published_at=None,
                rank_position=None,
                doi=None,
                journal=domain,
                open_access=None,
                engagement={},
                raw_payload={"fallback": "title_tag"},
            )

    logger.warning("playwright_crawl: all extraction strategies failed for %s", url)
    return None


def _from_jsonld(soup: BeautifulSoup, url: str, domain: str) -> CollectedItem | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = next(
                    (d for d in data if d.get("@type") in ("Article", "NewsArticle", "BlogPosting", "WebPage")),
                    data[0] if data else {},
                )

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
                source="playwright_crawl",
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
        source="playwright_crawl",
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
        source="playwright_crawl",
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


def _get_meta_description(soup: BeautifulSoup) -> str | None:
    for attr in ({"name": "description"}, {"property": "og:description"}):
        tag = soup.find("meta", attrs=attr)
        if tag and tag.get("content"):
            return _clean_text(tag["content"])
    return None


def _clean_text(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None
