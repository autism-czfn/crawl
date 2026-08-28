"""Chunk pipeline: splits crawled content_body into chunks and stores them.

Runs periodically, finds CrawledItems with content_body but no chunks,
splits them, and inserts chunk rows.

Sprint 1-B: chunk rows are enriched with metadata from the parent crawled_item.
Sprint 1-C: items flagged needs_rechunk=True have their old chunks deleted first.
Sprint 4-A/B: a summary chunk (chunk_index=-1, is_summary=True) is inserted for
              parent-child retrieval using title + first 500 chars of content.
"""
from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime, timezone

from sqlalchemy import select, func, delete, update
from sqlalchemy.dialects.postgresql import insert

from src.chunker import chunk_text
from src.storage.db import AsyncSessionLocal
from src.storage.models import CrawledItem, Chunk

logger = logging.getLogger(__name__)

_BATCH_SIZE = 200    # increased from 50 to clear backlog faster
_INTERVAL_SEC = 900  # 15 minutes


def _extract_domain(url: str | None) -> str | None:
    """Extract hostname from URL for the domain metadata column."""
    if not url:
        return None
    try:
        return urllib.parse.urlparse(url).netloc or None
    except Exception:
        return None


async def run_once(max_items: int = _BATCH_SIZE) -> int:
    """Find items with content_body but no chunks, chunk them, store chunks.

    Also processes items where needs_rechunk=True (content changed).
    """
    total_chunked = 0

    async with AsyncSessionLocal() as session:
        # --- Pass 1: items flagged for re-chunking (content changed) ---
        rechunk_result = await session.execute(
            select(
                CrawledItem.id,
                CrawledItem.content_body,
                CrawledItem.title,
                CrawledItem.url,
                CrawledItem.source_type,
                CrawledItem.authority_tier,
                CrawledItem.published_at,
                CrawledItem.lang,
                CrawledItem.domain_tags,
                CrawledItem.topic_tags,
            )
            .where(CrawledItem.needs_rechunk == True)  # noqa: E712
            .where(CrawledItem.content_body.isnot(None))
            .where(CrawledItem.content_body != "")
            .order_by(CrawledItem.collected_at.desc())
            .limit(max_items)
        )
        rechunk_rows = rechunk_result.fetchall()

        # --- Pass 2: items with content but no chunks yet ---
        chunked_ids = (
            select(Chunk.crawled_item_id)
            .where(Chunk.is_summary.is_(False) | Chunk.is_summary.is_(None))
            .distinct()
            .scalar_subquery()
        )
        new_result = await session.execute(
            select(
                CrawledItem.id,
                CrawledItem.content_body,
                CrawledItem.title,
                CrawledItem.url,
                CrawledItem.source_type,
                CrawledItem.authority_tier,
                CrawledItem.published_at,
                CrawledItem.lang,
                CrawledItem.domain_tags,
                CrawledItem.topic_tags,
            )
            .where(CrawledItem.content_body.isnot(None))
            .where(CrawledItem.content_body != "")
            .where(CrawledItem.id.notin_(chunked_ids))
            .where(CrawledItem.needs_rechunk == False)  # noqa: E712
            .order_by(CrawledItem.collected_at.desc())
            .limit(max_items)
        )
        new_rows = new_result.fetchall()

    # Combine: rechunk rows first, then new rows (deduplicated by id)
    seen_ids: set[int] = set()
    rows = []
    for row in list(rechunk_rows) + list(new_rows):
        if row.id not in seen_ids:
            seen_ids.add(row.id)
            rows.append(row)

    if not rows:
        return 0

    async with AsyncSessionLocal() as session:
        for row in rows:
            item_id = row.id
            content_body = row.content_body
            title = row.title
            url = row.url
            source_type = row.source_type
            authority_tier = row.authority_tier
            published_at = row.published_at
            lang = row.lang
            domain = _extract_domain(url)
            domain_tags = row.domain_tags
            topic_tags = row.topic_tags

            # BUG FIXED 2026-08-27: this whole per-item body used to run with
            # no isolation on the batch's one shared session — if ANY single
            # item's chunk_text (e.g. a stray NUL byte Postgres rejects
            # outright, or any other DB error) failed, the exception
            # propagated out of the entire `for row in rows:` loop and past
            # the final `session.commit()`, silently discarding chunking
            # work for EVERY item in the batch (up to 200), not just the bad
            # one — same failure class already found/fixed in save_items()
            # and enrich_fulltext(). A SAVEPOINT scopes any failure to just
            # this one item.
            try:
                async with session.begin_nested():
                    # Delete stale chunks if this is a re-chunk pass
                    if row in rechunk_rows:
                        await session.execute(
                            delete(Chunk).where(Chunk.crawled_item_id == item_id)
                        )

                    chunks = chunk_text(content_body)
                    if not chunks:
                        # Still clear the needs_rechunk flag
                        await session.execute(
                            update(CrawledItem)
                            .where(CrawledItem.id == item_id)
                            .values(needs_rechunk=False)
                        )
                        continue

                    # Insert regular chunks with metadata
                    # TODO: section_heading and heading_path will be populated in a future sprint
                    # when raw HTML is stored. Currently content_body is plain text (HTML stripped),
                    # so heading structure cannot be re-extracted at chunk time.
                    for idx, chunk_text_val in enumerate(chunks):
                        stmt = insert(Chunk).values(
                            crawled_item_id=item_id,
                            chunk_index=idx,
                            chunk_text=chunk_text_val,
                            title=title,
                            url=url,
                            domain=domain,
                            source_type=source_type,
                            authority_tier=authority_tier,
                            published_at=published_at,
                            lang=lang,
                            domain_tags=domain_tags,
                            topic_tags=topic_tags,
                            is_summary=False,
                            section_heading=None,
                            heading_path=None,
                        )
                        # Skip if chunk already exists (idempotent for new-row pass)
                        stmt = stmt.on_conflict_do_nothing()
                        await session.execute(stmt)

                    # Sprint 4-A/B: insert summary chunk (document-level embedding anchor)
                    summary_text = f"{title or ''} {content_body[:500]}".strip()
                    if summary_text:
                        summary_stmt = insert(Chunk).values(
                            crawled_item_id=item_id,
                            chunk_index=-1,
                            chunk_text=summary_text,
                            title=title,
                            url=url,
                            domain=domain,
                            source_type=source_type,
                            authority_tier=authority_tier,
                            published_at=published_at,
                            lang=lang,
                            domain_tags=domain_tags,
                            topic_tags=topic_tags,
                            is_summary=True,
                            section_heading=None,
                            heading_path=None,
                        )
                        summary_stmt = summary_stmt.on_conflict_do_nothing()
                        await session.execute(summary_stmt)

                    # Clear needs_rechunk flag
                    await session.execute(
                        update(CrawledItem)
                        .where(CrawledItem.id == item_id)
                        .values(needs_rechunk=False)
                    )

                    total_chunked += 1
            except Exception as exc:
                logger.warning("chunk_pipeline: failed for item %s, skipping just this item: %s", item_id, exc)
                continue

        await session.commit()

    if total_chunked:
        logger.info("Chunked %d items", total_chunked)
    return total_chunked


async def run_loop() -> None:
    """Long-running loop that chunks items periodically."""
    import asyncio
    logger.info("Chunk pipeline started (interval=%ds)", _INTERVAL_SEC)
    while True:
        try:
            await run_once()
        except Exception as exc:
            logger.error("Chunk pipeline error: %s", exc)
        await asyncio.sleep(_INTERVAL_SEC)
