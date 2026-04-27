import asyncio
import logging
from src.config import settings
from src.captions import run_loop as caption_loop
from src.chunk_pipeline import run_loop as chunk_loop
from src.embeddings import run_loop as embedding_loop
from src.pipeline import enrich_fulltext_loop
from src.scheduler import Scheduler

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL, logging.INFO))
logger = logging.getLogger(__name__)


async def main() -> None:
    logger.info("Starting autism-crawler")
    scheduler = Scheduler()
    await asyncio.gather(
        scheduler.run(),
        embedding_loop(),
        chunk_loop(),
        caption_loop(),
        enrich_fulltext_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
