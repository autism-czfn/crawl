"""Lightweight HTTP health server for the crawl worker, polled by the LAN
service-monitor (~/data/code/monitor). Exposes GET /api/health.

Two independent signals feed it:
  - DB reachability: a cheap `SELECT 1` against the async engine.
  - Loop liveness: each long-running asyncio task in main.py calls
    heartbeat(<name>) once per iteration (success OR caught exception — either
    means the task is still alive and cycling). health() flags any task whose
    last heartbeat is older than its registered staleness window as "stuck" —
    the failure mode a plain process-alive check (or systemd, if it applied
    here) can't see, because the process never exits, it just wedges.

Returns 200 when DB is reachable and no loop is stuck; 503 otherwise, with a
body naming which check failed. monitor's checks/http.py treats any non-200 as
"not_ready" and any connection failure as "down" — both are the right
classification here (503 = alive but degraded; connection refused = actually
down).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from src.storage.db import engine

logger = logging.getLogger(__name__)

# Keep in sync with setup.sh's FULL_TEXT_MIN_LEN (option 10, "FullTxt
# Downloaded") — same threshold and definition (length(content_body) >=
# this, any source) so this endpoint's numbers agree with that report.
FULL_TEXT_MIN_LEN = 1000

# The two /api/health article counts are seq-scan-or-worse queries over all
# of crawled_items (no index supports either predicate — see the discussion
# that led here). Recomputing them on every request would mean the LAN
# monitor's 10s poll pays a full-table scan every 10s, competing with the
# crawler's own hot-path queries on the same table as it grows. Instead a
# background loop refreshes a cached value on its own cadence and requests
# just read it.
_ARTICLE_COUNTS_REFRESH_SECONDS = 60

_HEARTBEATS: Dict[str, "LoopHeartbeat"] = {}

# Set to the time.time() a not-ready result first appeared, so the next ok
# result can log how long the outage actually lasted; None while healthy.
# Otherwise an incident like INC-20260912-0003 (503 for ~2 min, self-resolved)
# leaves zero trace in our own log — the only record is the external
# monitor's alert, and answering "why" means guessing from unrelated lines
# around the alert's timestamp instead of reading what actually tripped it.
_unhealthy_since: float | None = None


@dataclass
class LoopHeartbeat:
    max_staleness_seconds: float
    last_at: float = field(default_factory=time.time)


@dataclass
class ArticleCounts:
    full_articles_total: int | None = None
    full_articles_last_24h: int | None = None
    updated_at: float | None = None


_article_counts = ArticleCounts()


def register(name: str, expected_interval_seconds: float, stale_multiplier: float = 3.0) -> None:
    """Call once, right before a loop's `while True:`. stale_multiplier gives
    slow iterations (a big surface batch, a slow upstream API) room before
    being flagged — tune per loop if 3x its own interval is too tight/loose."""
    _HEARTBEATS[name] = LoopHeartbeat(max_staleness_seconds=expected_interval_seconds * stale_multiplier)


def heartbeat(name: str) -> None:
    """Call once per loop iteration, after the try/except, before the sleep."""
    hb = _HEARTBEATS.get(name)
    if hb is None:
        # tolerate a heartbeat() firing before register() during startup ordering
        hb = _HEARTBEATS[name] = LoopHeartbeat(max_staleness_seconds=600)
    hb.last_at = time.time()


async def _db_ok() -> tuple[bool, str]:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True, "connected"
    except Exception as exc:
        return False, ("unreachable: " + str(exc))[:200]


async def _refresh_article_counts() -> None:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                f"""
                SELECT
                    count(*) FILTER (WHERE length(content_body) >= {FULL_TEXT_MIN_LEN}) AS total,
                    count(*) FILTER (
                        WHERE length(content_body) >= {FULL_TEXT_MIN_LEN}
                          AND collected_at > now() - interval '24 hours'
                    ) AS last_24h
                FROM crawled_items
                """
            )
        )
        row = result.one()
    _article_counts.full_articles_total = row.total
    _article_counts.full_articles_last_24h = row.last_24h
    _article_counts.updated_at = time.time()


async def _article_counts_loop() -> None:
    """Background refresh for the /api/health article counts — see the
    module-level comment on _ARTICLE_COUNTS_REFRESH_SECONDS for why these
    aren't computed inline per request."""
    register("health_article_counts", _ARTICLE_COUNTS_REFRESH_SECONDS)
    while True:
        try:
            await _refresh_article_counts()
        except Exception:
            logger.exception("article-count refresh failed")
        heartbeat("health_article_counts")
        await asyncio.sleep(_ARTICLE_COUNTS_REFRESH_SECONDS)


def create_app() -> FastAPI:
    app = FastAPI(title="autism-crawler health")

    @app.get("/api/health")
    async def health():
        global _unhealthy_since
        now = time.time()
        db_ok, db_detail = await _db_ok()
        ages = {name: round(now - hb.last_at, 1) for name, hb in _HEARTBEATS.items()}
        stuck = {name: age for name, age in ages.items() if age > _HEARTBEATS[name].max_staleness_seconds}
        ok = db_ok and not stuck
        counts_age = (round(now - _article_counts.updated_at, 1)
                      if _article_counts.updated_at is not None else None)
        body = {"status": "ok" if ok else "not_ready", "db": db_detail,
                "loops_age_seconds": ages, "stuck_loops": stuck,
                "full_articles_total": _article_counts.full_articles_total,
                "full_articles_last_24h": _article_counts.full_articles_last_24h,
                "full_articles_counts_age_seconds": counts_age}

        if not ok:
            if _unhealthy_since is None:
                _unhealthy_since = now
            logger.warning(
                "health check not_ready: db_ok=%s db=%s stuck_loops=%s",
                db_ok, db_detail, stuck,
            )
        elif _unhealthy_since is not None:
            logger.warning(
                "health check recovered after %.0fs (was not_ready since %s)",
                now - _unhealthy_since,
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_unhealthy_since)),
            )
            _unhealthy_since = None

        return JSONResponse(status_code=200 if ok else 503, content=body)

    return app


async def serve(host: str, port: int) -> None:
    import logging
    import uvicorn
    asyncio.create_task(_article_counts_loop())
    config = uvicorn.Config(create_app(), host=host, port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # main.py owns SIGTERM/SIGINT, same reasoning as monitor/monitor.py _serve_api
    logging.getLogger(__name__).info("Health API on http://%s:%d/api/health", host, port)
    await server.serve()
