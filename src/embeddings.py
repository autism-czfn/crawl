"""Embedding pipeline: generates and stores pgvector embeddings for crawled items.

Schedule: run every 15 minutes, process up to 500 new items per batch.
Embed text: title + ' ' + description[:500]
Model: nomic-ai/nomic-embed-text-v1.5 (768 dims, local via fastembed — no API key required)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from src.embedder import MODEL_NAME, embed_texts
from src.storage.db import AsyncSessionLocal
from src.storage.models import Chunk, CrawledItem

logger = logging.getLogger(__name__)

_BATCH_SIZE = 100
_MAX_PER_RUN = 500
_INTERVAL_SEC = 900  # 15 minutes


async def run_once(max_items: int = _MAX_PER_RUN) -> int:
    """Generate embeddings for items missing them. Returns count of items embedded."""
    total_embedded = 0

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(CrawledItem.id, CrawledItem.title, CrawledItem.description, CrawledItem.content_body)
            .where(CrawledItem.embedding.is_(None))
            .order_by(CrawledItem.collected_at.desc())
            .limit(max_items)
        )
        rows = result.fetchall()

    if not rows:
        return 0

    # Process in batches of _BATCH_SIZE
    for batch_start in range(0, len(rows), _BATCH_SIZE):
        batch = rows[batch_start : batch_start + _BATCH_SIZE]
        ids = [r.id for r in batch]
        texts = [_embed_text(r.title, r.description, r.content_body) for r in batch]

        try:
            # fastembed is synchronous — run in thread to avoid blocking the event loop
            vectors = await asyncio.to_thread(embed_texts, texts)
        except Exception as exc:
            logger.warning(
                "Embedding failed for batch of %d items; will retry next run: %s",
                len(batch), exc,
            )
            break

        async with AsyncSessionLocal() as session:
            for row_id, vector in zip(ids, vectors):
                await session.execute(
                    update(CrawledItem)
                    .where(CrawledItem.id == row_id)
                    .values(
                        embedding=vector,
                        embedding_model=MODEL_NAME,
                        embedded_at=datetime.now(tz=timezone.utc),
                    )
                )
            await session.commit()

        total_embedded += len(batch)
        logger.info("Embedded %d items (total this run: %d)", len(batch), total_embedded)

    return total_embedded


async def run_loop() -> None:
    """Long-running loop that calls run_once every 15 minutes."""
    logger.info("Embedding loop started (interval=%ds)", _INTERVAL_SEC)
    while True:
        try:
            count = await run_once()
            if count:
                logger.info("Embedding run complete: %d items embedded", count)
            chunk_count = await run_once_chunks()
            if chunk_count:
                logger.info("Chunk embedding run complete: %d chunks embedded", chunk_count)
        except Exception as exc:
            logger.error("Embedding loop error: %s", exc)
        await asyncio.sleep(_INTERVAL_SEC)


async def run_once_chunks(max_items: int = _MAX_PER_RUN) -> int:
    """Generate embeddings for chunks missing them. Returns count embedded."""
    total_embedded = 0

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Chunk.id, Chunk.chunk_text)
            .where(Chunk.embedding.is_(None))
            .order_by(Chunk.id.desc())
            .limit(max_items)
        )
        rows = result.fetchall()

    if not rows:
        return 0

    for batch_start in range(0, len(rows), _BATCH_SIZE):
        batch = rows[batch_start : batch_start + _BATCH_SIZE]
        ids = [r.id for r in batch]
        texts = [r.chunk_text[:2000] for r in batch]

        try:
            vectors = await asyncio.to_thread(embed_texts, texts)
        except Exception as exc:
            logger.warning(
                "Chunk embedding failed for batch of %d chunks; will retry next run: %s",
                len(batch), exc,
            )
            break

        async with AsyncSessionLocal() as session:
            for row_id, vector in zip(ids, vectors):
                await session.execute(
                    update(Chunk)
                    .where(Chunk.id == row_id)
                    .values(
                        embedding=vector,
                        embedding_model=MODEL_NAME,
                        embedded_at=datetime.now(tz=timezone.utc),
                    )
                )
            await session.commit()

        total_embedded += len(batch)
        logger.info("Embedded %d chunks (total this run: %d)", len(batch), total_embedded)

    return total_embedded


def _embed_text(title: str, description: str | None, content_body: str | None = None) -> str:
    """Compose text for embedding. Prefers content_body over description."""
    parts = [title.strip()]
    if content_body:
        parts.append(content_body[:800].strip())
    elif description:
        parts.append(description[:500].strip())
    return " ".join(parts)
