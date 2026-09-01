import asyncio
import logging
import signal
from src.config import settings
from src.chunk_pipeline import run_loop as chunk_loop
from src.discovery.loop import discovery_loop
from src.discovery.search_queue_loop import search_queue_loop
from src.embeddings import subprocess_embedding_loop as embedding_loop
from src.pipeline import enrich_fulltext_loop, shutdown_pdf_pool
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

    # Plain SIGTERM (what a normal `kill <pid>` sends, and what setup.sh's
    # stop_existing() uses before its kill -9 fallback) has NO default
    # Python-level handler — it terminates the process immediately, before
    # any `finally` block or cleanup code runs. That's exactly what orphaned
    # the PDF process pool's workers on 2026-08-26 (see shutdown_pdf_pool's
    # docstring): the parent died, but its ProcessPoolExecutor children kept
    # running under PID 1 forever. Registering a handler here means SIGTERM
    # instead cancels the running tasks and lets us clean up before exiting.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    tasks = [
        asyncio.create_task(scheduler.run()),
        asyncio.create_task(embedding_loop()),
        asyncio.create_task(chunk_loop()),
        asyncio.create_task(enrich_fulltext_loop()),
        asyncio.create_task(log_health_metrics()),
        asyncio.create_task(discovery_loop()),
        asyncio.create_task(search_queue_loop()),
    ]

    await stop_event.wait()
    logger.info("Shutdown signal received — cancelling tasks and cleaning up...")

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    shutdown_pdf_pool()
    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
