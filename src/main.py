import asyncio
import logging
import platform
import re
import signal
import subprocess
from datetime import datetime, timedelta
from src.config import settings
from src.chunk_pipeline import run_loop as chunk_loop
from src.discovery.loop import discovery_loop
from src.discovery.search_queue_loop import search_queue_loop
from src.embeddings import subprocess_embedding_loop as embedding_loop
from src.health import serve as health_serve
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


def _system_boot_time_str() -> str:
    """Best-effort OS boot time, so a log reader can tell a signal-driven
    shutdown (SIGTERM from `kill`, or setup.sh's stop_existing()) apart from
    the process having been killed out from under itself by a machine
    reboot — the latter shows a boot time at (or a few seconds after) the
    prior "Shutdown signal received" timestamp, with no start line in
    between. Returns "unknown" rather than raising if the host doesn't
    support either lookup (e.g. inside some minimal containers).
    """
    try:
        if platform.system() == "Darwin":
            out = subprocess.check_output(
                ["sysctl", "-n", "kern.boottime"], text=True, timeout=5
            )
            m = re.search(r"sec = (\d+)", out)
            if m:
                return datetime.fromtimestamp(int(m.group(1))).strftime("%Y-%m-%d %H:%M:%S")
        elif platform.system() == "Linux":
            with open("/proc/uptime") as f:
                uptime_seconds = float(f.read().split()[0])
            return (datetime.now() - timedelta(seconds=uptime_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return "unknown"


async def main() -> None:
    logger.info("Starting autism-crawler (system boot time: %s)", _system_boot_time_str())
    scheduler = Scheduler()

    # Plain SIGTERM (what a normal `kill <pid>` sends, and what setup.sh's
    # stop_existing() uses before its kill -9 fallback) has NO default
    # Python-level handler — it terminates the process immediately, before
    # any `finally` block or cleanup code runs. That's exactly what orphaned
    # the PDF process pool's workers on 2026-08-26 (see shutdown_pdf_pool's
    # docstring): the parent died, but its ProcessPoolExecutor children kept
    # running under PID 1 forever. Registering a handler here means SIGTERM
    # instead cancels the running tasks and lets us clean up before exiting.
    #
    # We also record *which* signal fired: SIGTERM is what both a normal
    # `kill`/machine shutdown and setup.sh send, while SIGINT is Ctrl-C from
    # an interactive terminal — logging the name (and, above, the OS boot
    # time at startup) is what lets a future "why is it down?" be answered
    # by grepping this log instead of cross-referencing `last reboot`.
    stop_event = asyncio.Event()
    received_signal: dict[str, str] = {}
    loop = asyncio.get_running_loop()

    def _on_signal(sig: signal.Signals) -> None:
        received_signal["name"] = sig.name
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal, sig)

    tasks = [
        asyncio.create_task(scheduler.run()),
        asyncio.create_task(embedding_loop()),
        asyncio.create_task(chunk_loop()),
        asyncio.create_task(enrich_fulltext_loop()),
        asyncio.create_task(log_health_metrics()),
        asyncio.create_task(discovery_loop()),
        asyncio.create_task(search_queue_loop()),
        asyncio.create_task(health_serve(settings.HEALTH_HOST, settings.HEALTH_PORT)),
    ]

    await stop_event.wait()
    logger.info(
        "Shutdown signal received (%s) — cancelling tasks and cleaning up...",
        received_signal.get("name", "unknown"),
    )

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    shutdown_pdf_pool()
    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
