"""Embedding pipeline: generates and stores pgvector embeddings for crawled items.

Schedule: run every 15 minutes, process up to 500 new items per batch.
Embed text: title + ' ' + description[:500]
Model: nomic-ai/nomic-embed-text-v1.5 (768 dims, local via fastembed — no API key required)

Memory note: fastembed loads ~270 MB model + transformer inference can spike to several GB.
Batch sizes are kept small to avoid OOM kills on machines with limited RAM (≤8 GB).
"""
from __future__ import annotations

import asyncio
import gc
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from src.embedder import MODEL_NAME, embed_texts
from src.storage.db import AsyncSessionLocal
from src.storage.models import Chunk, CrawledItem

# Increment when chunking strategy or prefix convention changes.
# All embeddings with a different (or NULL) schema version will be re-generated.
EMBEDDING_SCHEMA_VERSION = "v2"

logger = logging.getLogger(__name__)

# Keep batches small to avoid OOM on ≤8 GB machines.
# fastembed + 100 long chunks can spike to ~7 GB; 20 keeps peak well under 2 GB.
_BATCH_SIZE = 20
_CHUNK_BATCH_SIZE = 5         # chunks are longer text — keep very small to avoid OOM on 7.4 GB host
_MAX_PER_RUN = 50             # reduced from 100; shorter runs = less chance of OOM from concurrent pressure
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
                        embedding_schema_version=EMBEDDING_SCHEMA_VERSION,
                    )
                )
            await session.commit()

        total_embedded += len(batch)
        logger.info("Embedded %d items (total this run: %d)", len(batch), total_embedded)

        # Release memory between batches to avoid OOM on low-RAM machines
        del vectors, texts, ids, batch
        gc.collect()

    return total_embedded


async def run_loop() -> None:
    """Long-running loop that calls run_once every 15 minutes (in-process).

    Prefer subprocess_embedding_loop() on memory-constrained hosts — it spawns
    a fresh process each run so the ONNX model memory is fully released between
    passes instead of staying resident for the life of the crawler.
    """
    logger.info("Embedding loop started (interval=%ds)", _INTERVAL_SEC)
    while True:
        try:
            count = await run_once()
            if count:
                logger.info("Embedding run complete: %d items embedded", count)
            logger.info("Embedding loop: starting chunk embedding pass")
            chunk_count = await run_once_chunks()
            if chunk_count:
                logger.info("Chunk embedding run complete: %d chunks embedded", chunk_count)
        except Exception as exc:
            logger.error("Embedding loop error: %s", exc, exc_info=True)
        await asyncio.sleep(_INTERVAL_SEC)


_MIN_FREE_MB = 4096  # require 4 GB free RAM before spawning embed_worker
_RAM_RETRY_SEC = 120  # wait 2 min and re-check if RAM is insufficient


def _free_ram_mb() -> int:
    """Return available RAM in MB (MemAvailable from /proc/meminfo)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 9999  # assume plenty if we can't read


async def subprocess_embedding_loop() -> None:
    """Spawn embed_worker as a subprocess every 15 minutes.

    Each run gets a fresh Python process: FastEmbed + ONNX runtime load, embed,
    then exit — fully reclaiming model memory between passes.  The main crawler
    process stays lean (no model weights in its heap).

    stdout/stderr from the worker are forwarded to the crawler's logger so they
    appear in crawler.log alongside the rest of the service output.

    Before spawning, checks that at least _MIN_FREE_MB (4 GB) of RAM is
    available.  If not, waits _RAM_RETRY_SEC (2 min) and retries — this
    prevents the OOM killer from immediately killing the worker process.
    """
    import sys

    logger.info(
        "Subprocess embedding loop started (interval=%ds) — "
        "model memory released between runs",
        _INTERVAL_SEC,
    )
    # Brief startup delay: let the scheduler seed surfaces before the first
    # embedding pass so the worker finds items to embed immediately.
    await asyncio.sleep(30)

    while True:
        # Gate on available RAM before spawning — the nomic-embed model loads
        # ~4-6 GB into the worker process. On a 7.4 GB host with only ~150 MB
        # free, the OOM killer will immediately SIGKILL the worker (exit -9).
        free_mb = _free_ram_mb()
        if free_mb < _MIN_FREE_MB:
            logger.warning(
                "embed_worker skipped: only %d MB free RAM (need %d MB); "
                "retrying in %ds",
                free_mb, _MIN_FREE_MB, _RAM_RETRY_SEC,
            )
            await asyncio.sleep(_RAM_RETRY_SEC)
            continue

        logger.info("Spawning embed_worker subprocess (free RAM: %d MB) …", free_mb)
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "src.embed_worker",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # merge stderr into stdout
            )
            stdout, _ = await proc.communicate()  # wait for worker to finish
            for line in (stdout or b"").decode(errors="replace").splitlines():
                if line.strip():
                    logger.info("[embed_worker] %s", line)
            if proc.returncode == -9:
                logger.error(
                    "embed_worker OOM-killed by kernel (SIGKILL -9); "
                    "free RAM was %d MB — will wait %ds before next attempt",
                    free_mb, _INTERVAL_SEC,
                )
            elif proc.returncode != 0:
                logger.error(
                    "embed_worker exited with non-zero code %d", proc.returncode
                )
            else:
                logger.info("embed_worker subprocess finished cleanly (model memory released)")
        except Exception as exc:
            logger.error("Failed to spawn embed_worker: %s", exc, exc_info=True)

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

    logger.info("Chunk embedding: %d chunks queued for embedding", len(rows))
    if not rows:
        return 0

    for batch_start in range(0, len(rows), _CHUNK_BATCH_SIZE):
        batch = rows[batch_start : batch_start + _CHUNK_BATCH_SIZE]
        ids = [r.id for r in batch]
        # nomic-embed-text-v1.5 handles up to 8192 tokens natively, but long chunks
        # consume significant GPU/CPU memory — keep batches small to avoid OOM.
        texts = [r.chunk_text for r in batch]

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
                        embedding_schema_version=EMBEDDING_SCHEMA_VERSION,
                    )
                )
            await session.commit()

        total_embedded += len(batch)
        logger.info("Embedded %d chunks (total this run: %d)", len(batch), total_embedded)

        # Release embedding vectors and input texts immediately to free RAM between batches
        del vectors, texts, ids, batch
        gc.collect()

    return total_embedded


def _embed_text(title: str, description: str | None, content_body: str | None = None) -> str:
    """Compose text for embedding. Prefers content_body over description."""
    parts = [title.strip()]
    if content_body:
        parts.append(content_body[:800].strip())
    elif description:
        parts.append(description[:500].strip())
    return " ".join(parts)
