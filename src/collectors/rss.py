"""Generic RSS/Atom feed collector using feedparser.

Sprint P3-F: ETag/Last-Modified caching — avoids redundant fetches by
storing and sending HTTP conditional request headers.
"""
from __future__ import annotations

import hashlib
import logging
from email.utils import parsedate_to_datetime

import feedparser

from src.collectors.base import CollectedItem
from src.http.client import get_shared_client

logger = logging.getLogger(__name__)


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


async def _get_cache(session, url: str):
    """Return (etag, last_modified) from HttpCache, or (None, None)."""
    from src.storage.models import HttpCache
    row = await session.get(HttpCache, _url_hash(url))
    if row:
        return row.etag, row.last_modified
    return None, None


async def _set_cache(session, url: str, etag: str | None, last_modified: str | None) -> None:
    """Upsert etag/last_modified for url into HttpCache."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from src.storage.models import HttpCache
    stmt = pg_insert(HttpCache).values(
        url_hash=_url_hash(url),
        etag=etag,
        last_modified=last_modified,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["url_hash"],
        set_={"etag": stmt.excluded.etag, "last_modified": stmt.excluded.last_modified},
    )
    await session.execute(stmt)
    await session.commit()


async def collect(
    config: dict,
    cursor: str | None,
    limit: int,
) -> tuple[list[CollectedItem], str | None]:
    """
    config keys:
      feeds: list[str]   — list of RSS feed URLs
    cursor: ISO8601 datetime of the most recent item already stored (skip older)
    """
    feeds: list[str] = config["feeds"]
    client = get_shared_client()

    all_entries: list[tuple[str, feedparser.FeedParserDict, feedparser.FeedParserDict]] = []

    for feed_url in feeds:
        try:
            # P3-F: check HttpCache for stored ETag/Last-Modified
            from src.storage.db import AsyncSessionLocal
            etag: str | None = None
            last_modified: str | None = None
            async with AsyncSessionLocal() as cache_session:
                etag, last_modified = await _get_cache(cache_session, feed_url)

            headers: dict[str, str] = {}
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified

            resp = await client.get(feed_url, headers=headers if headers else None)

            # 304 Not Modified — nothing new
            if resp.status_code == 304:
                logger.debug("RSS feed not modified (304): %s", feed_url)
                continue

            # Store new ETag / Last-Modified for next poll
            new_etag = resp.headers.get("ETag") or resp.headers.get("etag")
            new_lm = resp.headers.get("Last-Modified") or resp.headers.get("last-modified")
            if new_etag or new_lm:
                async with AsyncSessionLocal() as cache_session:
                    await _set_cache(cache_session, feed_url, new_etag, new_lm)

            parsed = feedparser.parse(resp.text)
        except Exception as exc:
            logger.error("RSS fetch failed for %s: %s", feed_url, exc)
            continue

        for entry in parsed.entries:
            all_entries.append((feed_url, parsed.feed, entry))

    # Sort by published descending so we process newest first
    def _pub(entry: feedparser.FeedParserDict) -> float:
        try:
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                import time
                return time.mktime(entry.published_parsed)
        except Exception:
            pass
        return 0.0

    all_entries.sort(key=lambda t: _pub(t[2]), reverse=True)

    items: list[CollectedItem] = []
    new_cursor: str | None = None

    for _, feed_meta, entry in all_entries:
        if len(items) >= limit:
            break

        url = entry.get("link", "")
        if not url:
            continue

        title = entry.get("title", "").strip()
        if not title:
            continue

        published_at: str | None = None
        try:
            if hasattr(entry, "published") and entry.published:
                published_at = parsedate_to_datetime(entry.published).isoformat()
        except Exception:
            pass

        # Skip items older than cursor
        if cursor and published_at and published_at <= cursor:
            continue

        if new_cursor is None and published_at:
            new_cursor = published_at

        description = entry.get("summary") or entry.get("description")
        if description:
            # Strip HTML tags for plain-text description
            import re
            description = re.sub(r"<[^>]+>", "", description).strip() or None

        author: str | None = None
        if hasattr(entry, "author"):
            author = entry.author or None

        items.append(
            CollectedItem(
                title=title,
                url=url,
                source="rss",
                external_id=entry.get("id") or entry.get("guid"),
                description=description,
                content_body=None,
                author=author,
                authors_json=None,
                published_at=published_at,
                rank_position=None,
                doi=None,
                journal=feed_meta.get("title"),
                open_access=None,
                engagement={},
                raw_payload=dict(entry),
            )
        )

    # Secondary pass: fetch full article body for each item
    from src.extractors.html import extract_body
    fetched = skipped = 0
    for item in items:
        url = item.get("url", "")
        if not url:
            continue
        try:
            art_resp = await client.get(url, use_browser_ua=True)
            from bs4 import BeautifulSoup
            art_soup = BeautifulSoup(art_resp.text, "html.parser")
            body = extract_body(art_soup)
            if body:
                item["content_body"] = body
                fetched += 1
            else:
                skipped += 1
                logger.warning("RSS body too short/boilerplate, skipped: %s", url)
        except Exception as exc:
            skipped += 1
            logger.warning("RSS article body fetch failed for %s: %s", url, exc)
    if fetched or skipped:
        logger.info("RSS secondary fetch: %d bodies extracted, %d failed/skipped", fetched, skipped)

    return items, new_cursor or cursor
