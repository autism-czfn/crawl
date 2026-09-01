"""Background discovery loop — crawl.txt section 13. Runs alongside the
existing scheduler/embedding/chunk/enrich_fulltext loops (src/main.py),
one more asyncio.gather() participant (section 13.6: "并行,不是串行"),
never blocking or waiting on any of them.

Works through a round-robin queue of (domain, topic) pairs — built from
config/surfaces.json's tier1/2 html_crawl/playwright_crawl/sitemap
surfaces (src/discovery/queue.py) — one pair per hour, using claude -p's
WebSearch tool (src/discovery/query_generator.py) to find tier1/2 pages
that the official-API/sitemap/RSS/html_crawl layers (1-3) missed, then
landing survivors through the two-layer trust gate in
src/discovery/landing.py.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import update

from src.discovery.landing import get_recent_known_urls, land_candidates
from src.discovery.query_generator import find_candidate_urls
from src.discovery.queue import build_discovery_queue, surface_metadata_for_pair
from src.storage.db import AsyncSessionLocal
from src.storage.models import DiscoveryQueueState

logger = logging.getLogger(__name__)

# 1 hour between claude -p calls — crawl.txt section 13.2: this account's
# 5-hour-rolling/weekly Claude Max usage quota is shared across normal
# coding sessions AND this loop, so the real constraint is quota
# consumption, not per-call dollar cost (see crawl.txt's 2026-08-26
# correction). Chosen over the originally-planned 10 minutes for that
# reason; §13.2 log the rationale to reconsider if this proves too slow
# or still too heavy on the shared quota in practice.
_INTERVAL_SEC = 3600


async def _load_cursor(session) -> DiscoveryQueueState:
    state = await session.get(DiscoveryQueueState, 1)
    if state is None:
        # Migration 0022 seeds this row; stay defensive in case a fresh DB
        # ever runs discovery_loop() before/without that seed row somehow.
        state = DiscoveryQueueState(id=1, last_index=-1)
        session.add(state)
        await session.commit()
    return state


def _startup_delay_seconds(
    last_run_at: datetime | None,
    now: datetime,
    interval_sec: int = _INTERVAL_SEC,
) -> float:
    """How long discovery_loop() should wait before its first pair of THIS
    process lifetime, so a restart never runs claude -p sooner than
    interval_sec after the last real call.

    Restart-safe pacing — confirmed live and broken (2026-08-28): this
    process gets restarted often (no supervisor — setup.sh/a human kills
    and restarts it directly; observed 9 minutes apart in crawler.log),
    and every restart used to run a pair IMMEDIATELY, because the loop
    only paced "time since THIS process started", never consulting the
    persisted discovery_queue_state.last_run_at it already writes on every
    cycle. A restart cadence faster than interval_sec silently turned the
    "1 call/hour, to protect the shared Claude Max quota" design
    (crawl.txt section 13.2) into calling claude -p on every restart — the
    exact quota-burn problem that pacing exists to prevent.

    Returns 0 if there's no recorded last run, or it was already
    interval_sec or longer ago (run immediately in either case).
    """
    if last_run_at is None:
        return 0.0
    elapsed = (now - last_run_at).total_seconds()
    return max(0.0, interval_sec - elapsed)


async def discovery_loop() -> None:
    logger.info("discovery loop started (interval=%ds)", _INTERVAL_SEC)

    async with AsyncSessionLocal() as session:
        state = await _load_cursor(session)
    delay = _startup_delay_seconds(state.last_run_at, datetime.now(tz=timezone.utc))
    if delay > 0:
        logger.info(
            "discovery: waiting %.0fs before the first call this run (restart-safe pacing)",
            delay,
        )
        await asyncio.sleep(delay)

    while True:
        try:
            await _run_one_pair()
        except Exception as exc:
            logger.error("discovery loop error: %s", exc, exc_info=True)

        await asyncio.sleep(_INTERVAL_SEC)


async def _run_one_pair() -> None:
    queue = build_discovery_queue()
    if not queue:
        logger.warning(
            "discovery: queue is empty — no tier1/2 html_crawl/playwright_crawl/"
            "sitemap surface in config/surfaces.json currently has domain_tags set"
        )
        return

    async with AsyncSessionLocal() as session:
        state = await _load_cursor(session)
        next_index = (state.last_index + 1) % len(queue)
    domain, topic = queue[next_index]

    logger.info("discovery: pair %d/%d — (%s, %s)", next_index + 1, len(queue), domain, topic)

    known_urls = await get_recent_known_urls(domain)
    candidates = await find_candidate_urls(domain, topic, known_urls)

    authority_tier, source_type, audience_type = surface_metadata_for_pair(domain, topic)
    inserted = await land_candidates(candidates, domain, topic, authority_tier, source_type, audience_type)

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(DiscoveryQueueState)
            .where(DiscoveryQueueState.id == 1)
            .values(
                last_index=next_index,
                last_pair_domain=domain,
                last_pair_topic=topic,
                last_run_at=datetime.now(tz=timezone.utc),
            )
        )
        await session.commit()

    logger.info(
        "discovery: (%s, %s) done — %d candidate(s) from claude -p, %d landed",
        domain, topic, len(candidates), inserted,
    )
