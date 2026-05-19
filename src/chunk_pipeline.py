"""Chunk pipeline: splits crawled content_body into chunks and stores them.

Runs periodically, finds CrawledItems with content_body but no chunks,
splits them, and inserts chunk rows.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert

from src.chunker import chunk_text
from src.storage.db import AsyncSessionLocal
from src.storage.models import CrawledItem, Chunk

logger = logging.getLogger(__name__)

_BATCH_SIZE = 200    # increased from 50 to clear backlog faster
_INTERVAL_SEC = 900  # 15 minutes


async def run_once(max_items: int = _BATCH_SIZE) -> int:
    """Find items with content_body but no chunks, chunk them, store chunks."""
    total_chunked = 0

    async with AsyncSessionLocal() as session:
        # Find crawled items that have content_body but no chunks yet
        chunked_ids = (
            select(Chunk.crawled_item_id)
            .distinct()
            .scalar_subquery()
        )
        result = await session.execute(
            select(CrawledItem.id, CrawledItem.content_body)
            .where(CrawledItem.content_body.isnot(None))
            .where(CrawledItem.content_body != "")
            .where(CrawledItem.id.notin_(chunked_ids))
            .order_by(CrawledItem.collected_at.desc())
            .limit(max_items)
        )
        rows = result.fetchall()

    if not rows:
        return 0

    async with AsyncSessionLocal() as session:
        for item_id, content_body in rows:
            chunks = chunk_text(content_body)
            if not chunks:
                continue

            for idx, chunk_text_val in enumerate(chunks):
                stmt = insert(Chunk).values(
                    crawled_item_id=item_id,
                    chunk_index=idx,
                    chunk_text=chunk_text_val,
                )
                # Skip if chunk already exists (idempotent)
                stmt = stmt.on_conflict_do_nothing()
                await session.execute(stmt)

            total_chunked += 1

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
