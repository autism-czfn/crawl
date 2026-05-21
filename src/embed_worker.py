"""One-shot embedding worker — embed pending items+chunks, then exit.

The main crawler process spawns this as a subprocess every 15 minutes instead
of running FastEmbed in-process.  Because this script exits after each run,
the ONNX model (~270 MB weights + runtime tensors that can spike to several GB)
is fully released back to the OS between passes, keeping the crawler's RSS lean.

Usage (spawned by main.py — not meant to be called manually):
    python -m src.embed_worker
"""
from __future__ import annotations

import asyncio
import logging
import sys

from src.config import settings
from src.embeddings import run_once, run_once_chunks

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    stream=sys.stdout,
    format="%(levelname)s:%(name)s:%(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    try:
        item_count = await run_once()
        chunk_count = await run_once_chunks()
        logger.info(
            "embed_worker finished: %d items embedded, %d chunks embedded",
            item_count,
            chunk_count,
        )
    except Exception as exc:
        logger.error("embed_worker failed: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
