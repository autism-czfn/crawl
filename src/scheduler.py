"""Scheduler: polls surfaces on their configured intervals and dispatches collectors."""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update, func

from src.pipeline import save_items
from src.storage.db import AsyncSessionLocal
from src.storage.models import CrawledItem, Surface

logger = logging.getLogger(__name__)

# Map platform name → collector module path
_COLLECTOR_MAP: dict[str, str] = {
    "reddit": "src.collectors.reddit",
    "rss": "src.collectors.rss",
    "pubmed": "src.collectors.pubmed",
    "europepmc": "src.collectors.europepmc",
    "openalex": "src.collectors.openalex",
    "semanticscholar": "src.collectors.semanticscholar",
    "crossref": "src.collectors.crossref",
    "biorxiv": "src.collectors.biorxiv",
    "doaj": "src.collectors.doaj",
    "clinicaltrials": "src.collectors.clinicaltrials",
    "core": "src.collectors.core",
    "wikipedia": "src.collectors.wikipedia",
    "hackernews": "src.collectors.hackernews",
    "youtube": "src.collectors.youtube",
    "newsapi": "src.collectors.newsapi",
    "html_crawl": "src.collectors.html_crawl",
    "playwright_crawl": "src.collectors.playwright_crawl",
    "sitemap": "src.collectors.sitemap",
    "nhs_api": "src.collectors.nhs",
    "cdc_data": "src.collectors.cdc_data",
    "link_harvester": "src.collectors.link_harvester",
    "link_harvester_backfill": "src.collectors.link_harvester_backfill",
}

_SURFACES_JSON = Path(__file__).parent.parent / "config" / "surfaces.json"
_TICK_INTERVAL_SEC = 60            # scheduler main loop sleep
_STALENESS_DAYS = 7                # warn if Tier-1 surface has no new items this many days
_STALENESS_CHECK_INTERVAL = 3600   # check staleness once per hour

# Playwright launches a full Chromium subprocess (~700 MB–1 GB RSS each).
# Cap concurrent playwright_crawl runs to prevent OOM when many surfaces are due
# simultaneously (e.g. first run after a config change, or after 24-hour poll fires).
_PLAYWRIGHT_CONCURRENCY = 2
_playwright_semaphore: asyncio.Semaphore | None = None


class Scheduler:
    def __init__(self) -> None:
        self._running = True
        self._tick_count = 0

    async def run(self) -> None:
        global _playwright_semaphore
        _playwright_semaphore = asyncio.Semaphore(_PLAYWRIGHT_CONCURRENCY)
        await self._seed_surfaces()
        logger.info("Scheduler started")

        while self._running:
            await self._tick()
            self._tick_count += 1
            # Check Tier-1 staleness once per _STALENESS_CHECK_INTERVAL seconds
            if self._tick_count % (_STALENESS_CHECK_INTERVAL // _TICK_INTERVAL_SEC) == 0:
                try:
                    await self._check_tier1_staleness()
                except Exception as exc:
                    logger.error("Tier-1 staleness check failed: %s", exc)
            await asyncio.sleep(_TICK_INTERVAL_SEC)

    async def _seed_surfaces(self) -> None:
        """Load surfaces.json into DB on first run (no-op if already present)."""
        if not _SURFACES_JSON.exists():
            logger.warning("surfaces.json not found at %s", _SURFACES_JSON)
            return

        with open(_SURFACES_JSON) as f:
            surfaces_config = json.load(f)

        async with AsyncSessionLocal() as session:
            for s in surfaces_config:
                existing = await session.get(Surface, s["key"])
                if existing is None:
                    surface = Surface(
                        key=s["key"],
                        platform=s["platform"],
                        enabled=bool(s.get("enabled", 1)),  # accepts 1/0 or true/false
                        poll_interval_sec=s.get("poll_interval_sec", 3600),
                        max_items_per_run=s.get("max_items", 30),
                        config_json=s.get("config", {}),
                        authority_tier=s.get("authority_tier"),
                        source_type=s.get("source_type"),
                        audience_type=s.get("audience_type"),
                        language=s.get("language", "en"),
                        country=s.get("country"),
                        organization_name=s.get("organization_name"),
                    )
                    session.add(surface)
                    logger.info("Seeded surface: %s", s["key"])
                else:
                    # Upsert config fields from surfaces.json.
                    # Preserve runtime-only fields: last_run_at, last_cursor,
                    # consecutive_fails, force_recrawl, overrides_json.
                    file_config = s.get("config", {})
                    # DB overrides_json wins over file config values
                    effective_config = {**file_config, **(existing.overrides_json or {})}
                    await session.execute(
                        update(Surface)
                        .where(Surface.key == s["key"])
                        .values(
                            platform=s["platform"],
                            enabled=bool(s.get("enabled", existing.enabled)),  # accepts 1/0 or true/false
                            poll_interval_sec=s.get("poll_interval_sec", existing.poll_interval_sec),
                            max_items_per_run=s.get("max_items", existing.max_items_per_run),
                            config_json=effective_config,
                            authority_tier=s.get("authority_tier", existing.authority_tier),
                            source_type=s.get("source_type", existing.source_type),
                            audience_type=s.get("audience_type", existing.audience_type),
                            language=s.get("language", existing.language),
                            country=s.get("country", existing.country),
                            organization_name=s.get("organization_name", existing.organization_name),
                        )
                    )
                    logger.debug("Updated surface config: %s", s["key"])
            await session.commit()

    async def _tick(self) -> None:
        """Check all enabled surfaces and run those that are due.

        playwright_crawl surfaces are throttled by _playwright_semaphore
        (max _PLAYWRIGHT_CONCURRENCY at once) to avoid spawning many Chromium
        processes simultaneously and exhausting system RAM.
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Surface).where(Surface.enabled == True)  # noqa: E712
            )
            surfaces = result.scalars().all()

        now = datetime.now(tz=timezone.utc)
        tasks = []
        for surface in surfaces:
            if surface.force_recrawl or _is_due(surface, now):
                if surface.platform == "playwright_crawl":
                    tasks.append(asyncio.create_task(
                        self._run_surface_throttled(surface.key)
                    ))
                else:
                    tasks.append(asyncio.create_task(
                        self._run_surface(surface.key)
                    ))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_surface_throttled(self, surface_key: str) -> None:
        """Wrapper that acquires the playwright semaphore before running."""
        async with _playwright_semaphore:
            logger.debug("playwright_semaphore acquired for %s", surface_key)
            await self._run_surface(surface_key)

    async def _run_surface(self, surface_key: str) -> None:
        async with AsyncSessionLocal() as session:
            surface = await session.get(Surface, surface_key)
            if surface is None:
                return

            platform = surface.platform
            collector_mod_path = _COLLECTOR_MAP.get(platform)
            if not collector_mod_path:
                logger.error("No collector for platform '%s' (surface: %s)", platform, surface_key)
                return

            config = surface.config_json or {}
            cursor = None if surface.force_recrawl else surface.last_cursor
            limit = surface.max_items_per_run

            try:
                mod = importlib.import_module(collector_mod_path)
                items, next_cursor = await mod.collect(config, cursor, limit)
                count = await save_items(items, surface_key, session)

                await session.execute(
                    update(Surface)
                    .where(Surface.key == surface_key)
                    .values(
                        last_run_at=datetime.now(tz=timezone.utc),
                        last_cursor=next_cursor,
                        last_status="ok" if items else "empty",
                        last_error=None,
                        last_run_count=count,
                        consecutive_fails=0,
                        force_recrawl=False,
                    )
                )
                await session.commit()
                logger.info("Surface %s: %d new items", surface_key, count)

            except Exception as exc:
                await session.rollback()
                logger.error("Surface %s failed: %s", surface_key, exc)
                await session.execute(
                    update(Surface)
                    .where(Surface.key == surface_key)
                    .values(
                        last_run_at=datetime.now(tz=timezone.utc),
                        last_status="error",
                        last_error=str(exc)[:500],
                        last_run_count=0,
                        consecutive_fails=Surface.consecutive_fails + 1,
                    )
                )
                await session.commit()


    async def _check_tier1_staleness(self) -> None:
        """Warn if any Tier-1 surface has produced no new items in the last 7 days."""
        threshold = datetime.now(tz=timezone.utc) - timedelta(days=_STALENESS_DAYS)

        async with AsyncSessionLocal() as session:
            # Get all enabled Tier-1 surfaces
            result = await session.execute(
                select(Surface).where(
                    Surface.enabled == True,  # noqa: E712
                    Surface.authority_tier == 1,
                )
            )
            tier1_surfaces = result.scalars().all()

        for surface in tier1_surfaces:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(func.max(CrawledItem.collected_at)).where(
                        CrawledItem.surface_key == surface.key
                    )
                )
                last_collected = result.scalar_one_or_none()

            if last_collected is None:
                logger.warning(
                    "Tier-1 surface '%s' (%s) has NEVER produced items — "
                    "check collector configuration",
                    surface.key,
                    surface.organization_name or "unknown org",
                )
                continue  # no timestamp to compare; skip staleness check
            elif last_collected.tzinfo is None:
                last_collected = last_collected.replace(tzinfo=timezone.utc)

            if last_collected < threshold:
                days_stale = (datetime.now(tz=timezone.utc) - last_collected).days
                logger.warning(
                    "Tier-1 surface '%s' (%s) has not produced new items in %d days "
                    "(last item: %s) — check collector or re-enable Playwright",
                    surface.key,
                    surface.organization_name or "unknown org",
                    days_stale,
                    last_collected.date(),
                )


def _is_due(surface: Surface, now: datetime) -> bool:
    if surface.last_run_at is None:
        return True
    last = surface.last_run_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    elapsed = (now - last).total_seconds()
    return elapsed >= surface.poll_interval_sec


# ---------------------------------------------------------------------------
# P3-D: Crawl health metrics
# ---------------------------------------------------------------------------

async def log_health_metrics() -> None:
    """Emit structured JSON health metrics every hour."""
    import json
    metrics_logger = logging.getLogger("crawl.metrics")
    while True:
        try:
            from sqlalchemy import func
            from sqlalchemy import select as _select
            from src.storage.models import CrawledItem as _CrawledItem, Chunk as _Chunk, Surface as _Surface

            async with AsyncSessionLocal() as session:
                embedding_queue = await session.scalar(
                    _select(func.count()).select_from(_CrawledItem)
                    .where(_CrawledItem.embedding.is_(None))
                    .where(_CrawledItem.content_body.isnot(None))
                )
                chunk_queue = await session.scalar(
                    _select(func.count()).select_from(_CrawledItem)
                    .where(_CrawledItem.content_body.isnot(None))
                    .where(_CrawledItem.content_body != "")
                )
                fail_surfaces = await session.scalar(
                    _select(func.count()).select_from(_Surface)
                    .where(_Surface.consecutive_fails > 3)
                )

            metrics_logger.info(json.dumps({
                "metric": "crawl_health",
                "embedding_queue_depth": embedding_queue,
                "chunk_queue_depth": chunk_queue,
                "surfaces_failing": fail_surfaces,
            }))
        except Exception as exc:
            metrics_logger.error("Health metrics error: %s", exc)
        await asyncio.sleep(3600)
