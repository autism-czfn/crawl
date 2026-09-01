"""Reactive discovery-queue consumer — websearch.txt sections 十/十五/十九.

search repo's WebSearch fallback (triggered when its own RAG has no
answer for a live user query) writes candidate URLs directly into
search_discovery_requests via its own asyncpg pool — same Postgres
instance, no HTTP API (see migration 0023's docstring for why). This
loop is the other half: it polls that table, resolves each row's domain
against crawl's own tier1/2 surfaces.json (never trusting search's
source_domain claim, or even parsing it — the domain used for every
downstream decision is re-derived from the URL itself, the same "two
layers, don't trust the other side" principle landing.py already
applies elsewhere), and either lands it into crawled_items via
land_search_queue_url(), or, for a domain that isn't already tier1/2,
asks src/discovery/classifier.py (a Haiku `claude -p` call) whether it
should be — a confident yes gets written into config/surfaces.json by
src/discovery/surfaces_writer.py and lands in the same pass; anything
less is recorded out_of_scope with the model's reason (websearch.txt
十九 made automatic instead of a dead-end status nothing ever consumed).

Runs alongside discovery_loop() and everything else in src/main.py's
asyncio.gather() — one more independent task, never blocking or waiting
on any of them.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import func, select, update

from src.discovery.classifier import classify_domain
from src.discovery.landing import land_search_queue_url
from src.discovery.queue import surface_metadata_for_domain
from src.discovery.surfaces_writer import append_surface_entry, build_auto_surface_entry
from src.storage.db import AsyncSessionLocal
from src.storage.models import SearchDiscoveryRequest

logger = logging.getLogger(__name__)

# No claude -p call anywhere in this path — search already found the
# URL, this loop only fetches it. Unlike discovery_loop()'s hourly
# cadence (which exists purely to protect the shared Claude Max quota),
# there's no quota concern here, so this can run far more often.
_INTERVAL_SEC = 180
_BATCH_SIZE = 50

# A row stuck in 'processing' past this means the consumer died mid-batch
# (crawl gets restarted often with no supervisor — see loop.py's
# _startup_delay_seconds docstring for the same observation) — reset it
# back to pending rather than leaving it stranded forever.
_PROCESSING_TIMEOUT = timedelta(minutes=10)

# Safety net for rows that somehow never got picked up (consumer down a
# long time, or a huge backlog outrunning _BATCH_SIZE) — not expected to
# fire in normal operation, since pending→processed is minutes, not days.
_PENDING_TTL = timedelta(days=14)

# websearch.txt 15.2 — differentiated retry backoff by failure reason
# (land_one_url()'s `reason` return value). None means permanent: never
# retry automatically.
_RETRY_POLICY: dict[str, timedelta | None] = {
    "http_404": None,
    "robots_blocked": None,
    "unparseable_url": None,
    # Shouldn't actually happen here — the domain is already gated via
    # surface_metadata_for_domain() before land_search_queue_url() is
    # ever called — but land_one_url() re-checks regardless (defensive),
    # so this needs a policy entry too.
    "not_allowlisted": None,
    "http_403": timedelta(days=30),
    "fetch_failed": timedelta(days=1),
    "extract_failed": timedelta(days=1),
    # The classifier call itself failed (spawn/timeout/malformed output —
    # see classifier.py's fail-open contract), not that it rejected the
    # domain. Worth one more try tomorrow, same as a transient fetch.
    "classifier_unavailable": timedelta(days=1),
}
_HTTP_STATUS_FOR_REASON = {"http_404": 404, "http_403": 403}

# After this many failed attempts, stop retrying even a nominally
# temporary failure — a domain that has 429/5xx'd 5 times running isn't
# going to start working on attempt 6.
_MAX_RETRY_COUNT = 5

# Auto-promotion safety valve (websearch.txt 十九, made automatic — see
# src/discovery/classifier.py + src/discovery/surfaces_writer.py): caps
# how many new surfaces.json entries one day's worth of high-confidence
# classifier "yes" answers can add, so a bad run of confident-but-wrong
# classifications can't flood the allowlist unattended. A row that hits
# the cap is recorded out_of_scope like any other reject — there's no
# reason to expect a differently-classified answer tomorrow, so this is a
# soft ceiling, not something rows wait in a queue for.
_MAX_AUTO_PROMOTIONS_PER_DAY = 5
_MIN_CONFIDENCE_TO_PROMOTE = "high"


async def search_queue_loop() -> None:
    logger.info("search discovery queue loop started (interval=%ds)", _INTERVAL_SEC)
    while True:
        try:
            await _run_one_cycle()
        except Exception as exc:
            logger.error("search queue loop error: %s", exc, exc_info=True)
        await asyncio.sleep(_INTERVAL_SEC)


async def _run_one_cycle() -> None:
    async with AsyncSessionLocal() as session:
        await _recover_stuck_processing(session)
        await _expire_stale_pending(session)
        await session.commit()

    rows = await _claim_batch()
    if not rows:
        return

    logger.info("search queue: processing %d row(s)", len(rows))
    for row_id, url, source_domain, title, snippet, trigger_query in rows:
        await _process_one(row_id, url, source_domain, title, snippet, trigger_query)


async def _recover_stuck_processing(session) -> None:
    cutoff = datetime.now(tz=timezone.utc) - _PROCESSING_TIMEOUT
    await session.execute(
        update(SearchDiscoveryRequest)
        .where(SearchDiscoveryRequest.status == "processing")
        .where(SearchDiscoveryRequest.discovered_at < cutoff)
        .values(status="pending")
    )


async def _expire_stale_pending(session) -> None:
    cutoff = datetime.now(tz=timezone.utc) - _PENDING_TTL
    await session.execute(
        update(SearchDiscoveryRequest)
        .where(SearchDiscoveryRequest.status == "pending")
        .where(SearchDiscoveryRequest.discovered_at < cutoff)
        .values(status="failed", error_note="stale_ttl_exceeded", next_retry_at=None)
    )


async def _claim_batch() -> list[tuple[int, str, str | None, str | None, str | None, str | None]]:
    """Selects up to _BATCH_SIZE due rows (fresh pending, or failed rows
    whose next_retry_at has arrived) and immediately marks them
    'processing' in the same session, so a slow row in this batch can't
    get re-claimed by the next cycle before this one finishes with it."""
    now = datetime.now(tz=timezone.utc)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(
                SearchDiscoveryRequest.id,
                SearchDiscoveryRequest.url,
                SearchDiscoveryRequest.source_domain,
                SearchDiscoveryRequest.title,
                SearchDiscoveryRequest.snippet,
                SearchDiscoveryRequest.trigger_query,
            )
            .where(
                (SearchDiscoveryRequest.status == "pending")
                | (
                    (SearchDiscoveryRequest.status == "failed")
                    & SearchDiscoveryRequest.next_retry_at.is_not(None)
                    & (SearchDiscoveryRequest.next_retry_at <= now)
                )
            )
            .order_by(SearchDiscoveryRequest.discovered_at)
            .limit(_BATCH_SIZE)
        )
        rows = result.all()
        if not rows:
            return []

        ids = [row[0] for row in rows]
        await session.execute(
            update(SearchDiscoveryRequest)
            .where(SearchDiscoveryRequest.id.in_(ids))
            .values(status="processing")
        )
        await session.commit()

    return [(row[0], row[1], row[2], row[3], row[4], row[5]) for row in rows]


def _domain_from_url(url: str) -> str | None:
    """The ONLY domain value ever used for the tier1/2 lookup — always
    re-derived from the URL itself, never from the source_domain column
    search wrote. search's source_domain is contract data (websearch.txt
    section 十) but is not trusted for the authority decision, same as
    landing.py never trusts a caller's own domain claim."""
    host = (urlparse(url).netloc or "").removeprefix("www.")
    return host or None


async def _process_one(
    row_id: int,
    url: str,
    source_domain: str | None,
    title: str | None,
    snippet: str | None,
    trigger_query: str | None,
) -> None:
    domain = _domain_from_url(url)
    if not domain:
        await _mark_failed(row_id, "unparseable_url")
        return

    if source_domain and source_domain.removeprefix("www.") != domain:
        logger.info(
            "search queue: row %d — search's source_domain=%s doesn't match "
            "the URL's actual host %s; using the actual host",
            row_id, source_domain, domain,
        )

    authority_tier, source_type, audience_type = surface_metadata_for_domain(domain)
    if authority_tier is None:
        await _try_classify_and_promote(row_id, url, domain, title, snippet, trigger_query)
        return

    outcome, reason = await land_search_queue_url(url, domain, authority_tier, source_type, audience_type)
    if outcome in ("landed", "already_known"):
        await _mark_done(row_id)
    else:
        await _mark_failed(row_id, reason)


async def _try_classify_and_promote(
    row_id: int,
    url: str,
    domain: str,
    title: str | None,
    snippet: str | None,
    trigger_query: str | None,
) -> None:
    """Called only when surface_metadata_for_domain(domain) found
    nothing — domain isn't already tier1/2. Asks the Haiku classifier
    (src/discovery/classifier.py) whether it should be; on a confident
    yes, writes it into surfaces.json (src/discovery/surfaces_writer.py)
    and lands the triggering URL in the same pass. Anything less than a
    confident yes is recorded out_of_scope with the model's reason — the
    same terminal status this domain would have gotten before this
    feature existed, just no longer silent about why."""
    async with AsyncSessionLocal() as session:
        promotions_today = await _count_promotions_last_24h(session)
    if promotions_today >= _MAX_AUTO_PROMOTIONS_PER_DAY:
        await _mark_out_of_scope(row_id, note="daily_cap_reached")
        return

    result = await classify_domain(domain, url, title, snippet, trigger_query)
    if result is None:
        await _mark_failed(row_id, "classifier_unavailable")
        return

    if result["tier"] not in (1, 2) or result["confidence"] != _MIN_CONFIDENCE_TO_PROMOTE:
        await _mark_out_of_scope(
            row_id,
            note=result["reason"],
            classifier_tier=result["tier"],
            classifier_confidence=result["confidence"],
            classifier_reason=result["reason"],
        )
        return

    entry = build_auto_surface_entry(
        domain,
        result["tier"],
        result["source_type"],
        result["audience_type"],
        result["reason"],
        datetime.now(tz=timezone.utc).date().isoformat(),
    )
    promoted = await asyncio.to_thread(append_surface_entry, entry)
    if not promoted:
        await _mark_out_of_scope(
            row_id,
            note="already_covered_or_invalid",
            classifier_tier=result["tier"],
            classifier_confidence=result["confidence"],
            classifier_reason=result["reason"],
        )
        return

    await _stamp_classifier_fields(
        row_id, result["tier"], result["confidence"], result["reason"], entry["key"],
    )

    # Re-resolve rather than trusting the classifier's own tier/source_type/
    # audience_type directly — surfaces.json (just written) is the single
    # source of truth every other lookup in this module uses, same "don't
    # trust the other side" principle as everywhere else here.
    authority_tier, source_type, audience_type = surface_metadata_for_domain(domain)
    outcome, reason = await land_search_queue_url(url, domain, authority_tier, source_type, audience_type)
    if outcome in ("landed", "already_known"):
        await _mark_done(row_id)
    else:
        await _mark_failed(row_id, reason)


async def _count_promotions_last_24h(session) -> int:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    result = await session.execute(
        select(func.count())
        .select_from(SearchDiscoveryRequest)
        .where(SearchDiscoveryRequest.promoted_surface_key.is_not(None))
        .where(SearchDiscoveryRequest.processed_at > cutoff)
    )
    return result.scalar() or 0


async def _stamp_classifier_fields(
    row_id: int, tier: int, confidence: str, reason: str, promoted_surface_key: str,
) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(SearchDiscoveryRequest)
            .where(SearchDiscoveryRequest.id == row_id)
            .values(
                classifier_tier=tier,
                classifier_confidence=confidence,
                classifier_reason=reason,
                promoted_surface_key=promoted_surface_key,
            )
        )
        await session.commit()


def _compute_retry_update(
    reason: str | None,
    retry_count_before: int,
    now: datetime,
) -> tuple[int, datetime | None]:
    """Pure (no DB, no I/O — explicit `now` for testability, same pattern
    as discovery/loop.py::_startup_delay_seconds). Given a failure reason
    and the retry_count before this attempt, returns (new_retry_count,
    next_retry_at). next_retry_at=None means permanent: either the reason
    itself never retries (websearch.txt 15.2), or _MAX_RETRY_COUNT was
    reached."""
    retry_count = retry_count_before + 1
    delay = _RETRY_POLICY.get(reason)
    if delay is None or retry_count >= _MAX_RETRY_COUNT:
        return retry_count, None
    return retry_count, now + delay


async def _mark_done(row_id: int) -> None:
    now = datetime.now(tz=timezone.utc)
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(SearchDiscoveryRequest)
            .where(SearchDiscoveryRequest.id == row_id)
            .values(status="done", processed_at=now, error_note=None, next_retry_at=None)
        )
        await session.commit()


async def _mark_out_of_scope(
    row_id: int,
    note: str | None = None,
    classifier_tier: int | None = None,
    classifier_confidence: str | None = None,
    classifier_reason: str | None = None,
) -> None:
    now = datetime.now(tz=timezone.utc)
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(SearchDiscoveryRequest)
            .where(SearchDiscoveryRequest.id == row_id)
            .values(
                status="out_of_scope",
                processed_at=now,
                next_retry_at=None,
                error_note=note,
                classifier_tier=classifier_tier,
                classifier_confidence=classifier_confidence,
                classifier_reason=classifier_reason,
            )
        )
        await session.commit()


async def _mark_failed(row_id: int, reason: str | None) -> None:
    now = datetime.now(tz=timezone.utc)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(SearchDiscoveryRequest.retry_count).where(SearchDiscoveryRequest.id == row_id)
        )
        row = result.first()
        retry_count_before = row[0] if row else 0
        retry_count, next_retry_at = _compute_retry_update(reason, retry_count_before, now)

        await session.execute(
            update(SearchDiscoveryRequest)
            .where(SearchDiscoveryRequest.id == row_id)
            .values(
                status="failed",
                processed_at=now,
                retry_count=retry_count,
                last_http_status=_HTTP_STATUS_FOR_REASON.get(reason),
                next_retry_at=next_retry_at,
                error_note=reason,
            )
        )
        await session.commit()
