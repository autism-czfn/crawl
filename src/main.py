import asyncio
import logging
from src.config import settings
from src.chunk_pipeline import run_loop as chunk_loop
from src.embeddings import subprocess_embedding_loop as embedding_loop
from src.pipeline import enrich_fulltext_loop
from src.scheduler import Scheduler, log_health_metrics

# captions.py (yt-dlp based) is disabled — YouTube transcripts are now fetched
# directly by the YouTube collector via youtube-transcript-api at collection time.

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    logger.info("Starting autism-crawler")
    scheduler = Scheduler()
    await asyncio.gather(
        scheduler.run(),
        embedding_loop(),
        chunk_loop(),
        enrich_fulltext_loop(),
        log_health_metrics(),
    )


if __name__ == "__main__":
    asyncio.run(main())
