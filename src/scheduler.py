"""Scheduler: polls surfaces on their configured intervals and dispatches collectors."""
import asyncio
import importlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update

from src.pipeline import save_items
from src.storage.db import AsyncSessionLocal
from src.storage.models import Surface

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
}

_SURFACES_JSON = Path(__file__).parent.parent / "config" / "surfaces.json"


class Scheduler:
    def __init__(self) -> None:
        self._running = True

    async def run(self) -> None:
        await self._seed_surfaces()
        logger.info("Scheduler started")

        while self._running:
            await self._tick()
            await asyncio.sleep(60)  # check every minute

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
                        enabled=s.get("enabled", True),
                        poll_interval_sec=s.get("poll_interval_sec", 3600),
                        max_items_per_run=s.get("max_items", 30),
                        config_json=s.get("config", {}),
                    )
                    session.add(surface)
                    logger.info("Seeded surface: %s", s["key"])
            await session.commit()

    async def _tick(self) -> None:
        """Check all enabled surfaces and run those that are due."""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Surface).where(Surface.enabled == True)  # noqa: E712
            )
            surfaces = result.scalars().all()

        now = datetime.now(tz=timezone.utc)
        tasks = []
        for surface in surfaces:
            if _is_due(surface, now):
                tasks.append(asyncio.create_task(self._run_surface(surface.key)))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
            cursor = surface.last_cursor
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


def _is_due(surface: Surface, now: datetime) -> bool:
    if surface.last_run_at is None:
        return True
    last = surface.last_run_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    elapsed = (now - last).total_seconds()
    return elapsed >= surface.poll_interval_sec
