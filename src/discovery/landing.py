"""Two-layer trust gate + landing for discovery-found URLs — crawl.txt
section 13.3 ("两层去重,不能只靠模型自觉") / 13.4 ("新发现的URL怎么落地").

claude -p's output (src/discovery/query_generator.py) is metadata-only —
a url + title, nothing else. This module decides what actually gets
fetched and stored, entirely in code:
  layer 2a — domain allowlist: the prompt's "site:<domain>" instruction
             is a soft constraint the model might not honor; re-verify
             the returned URL is really on that domain (or a subdomain).
  layer 2b — crawled_items dedup: normalize and check against the DB,
             regardless of what the model claimed about not repeating
             known URLs (the prompt-level list in query_generator.py is
             only layer 1, a soft hint to reduce wasted searches).
Only URLs that pass both get fetched, via the exact same single-page
extraction sitemap.py already uses (_extract_page) — no separate content
pipeline, no LLM-authored body/summary.
"""
from __future__ import annotations

import logging

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import or_, select

from src.collectors.sitemap import _extract_page
from src.http.client import get_shared_client
from src.pipeline import _normalize_url, save_items
from src.storage.db import AsyncSessionLocal
from src.storage.models import CrawledItem, Surface

logger = logging.getLogger(__name__)

# Distinguishes discovery-landed rows from every existing collector's
# platform-name source value (e.g. "html_crawl", "sitemap", "pubmed") —
# see crawl.txt discussion: "how many articles are from crawl vs
# claude websearch" is a `GROUP BY source` away with this in place.
DISCOVERY_SOURCE = "claude_websearch"

# Reactive path (websearch.txt 15.1/15.5) — search repo's own WebSearch
# fallback found this URL for a live user query, not discovery_loop()'s
# round-robin. Distinct from DISCOVERY_SOURCE so `GROUP BY source` on
# crawled_items tells proactive vs reactive discovery apart.
SEARCH_QUEUE_SOURCE = "search_websearch_queue"

_RECENT_URLS_LIMIT = 30


def _surface_key_for_pair(domain: str, topic: str | None) -> str:
    """Synthetic Surface key items landed via this module get attributed
    to. topic=None is the search-queue reactive path (websearch.txt
    section 15.1) — there's no (domain, topic) pair there, only a domain,
    so it gets its own per-domain key instead of discovery_<domain>_<topic>."""
    if topic is None:
        return f"discovery_search_{domain}"
    return f"discovery_{domain}_{topic}"


async def get_recent_known_urls(domain: str, limit: int = _RECENT_URLS_LIMIT) -> list[str]:
    """Layer 1 input (prompt-level, see query_generator._build_prompt) —
    most-recently-collected URLs already on this domain from ANY source
    (existing collectors or earlier discovery passes), newest first."""
    # Match the domain right after the scheme (with or without "www."),
    # not just as a substring anywhere in the URL — a bare "%domain%" LIKE
    # would also match e.g. an unrelated "notcdc.gov" or a query-string
    # value that happens to contain the same characters.
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(CrawledItem.url)
            .where(or_(
                CrawledItem.url.like(f"http://{domain}%"),
                CrawledItem.url.like(f"http://www.{domain}%"),
                CrawledItem.url.like(f"https://{domain}%"),
                CrawledItem.url.like(f"https://www.{domain}%"),
            ))
            .order_by(CrawledItem.collected_at.desc())
            .limit(limit)
        )
        return [row[0] for row in result.all()]


def _passes_domain_allowlist(url: str, expected_domain: str) -> bool:
    """Layer 2a — code-level, hard gate. Never trust the prompt alone."""
    from urllib.parse import urlparse

    host = (urlparse(url).netloc or "").removeprefix("www.")
    return host == expected_domain or host.endswith("." + expected_domain)


async def _ensure_discovery_surface(
    session,
    domain: str,
    topic: str | None,
    authority_tier: int | None,
    source_type: str | None,
    audience_type: str | None,
    source: str = DISCOVERY_SOURCE,
) -> str:
    """Upsert the synthetic Surface row this pair's landed items are
    attributed to — save_items() requires a real surfaces row to pull
    authority_tier/domain_tags/audience_type from (src/pipeline.py:281),
    and a websearch-found URL isn't itself any existing surface. Values
    are copied from whichever real tier1/2 surface(s) contributed this
    domain (or domain, topic pair) — queue.surface_metadata_for_pair()
    for the proactive path, queue.surface_metadata_for_domain() for the
    reactive one (websearch.txt 15.1) — not invented here.

    topic=None is the reactive path: there's no (domain, topic) pair, so
    domain_tags is left empty rather than guessing one (verified against
    src/pipeline.py::save_items() — _domain_tags is fully None/empty
    tolerant, no downstream code requires it non-empty).

    platform=source (not hardcoded) so a Surface row's platform column
    tells you at a glance which path created it — "claude_websearch"
    (proactive discovery_loop) vs "search_websearch_queue" (reactive
    search queue) — matching the CrawledItem.source distinction already
    used for the same purpose.

    enabled=False deliberately: this row exists purely as a metadata
    anchor for save_items(), not something the regular Scheduler should
    ever try to poll on its own. Confirmed live (2026-08-28) what
    enabled=True actually does — src/scheduler.py's _tick() runs every
    60s against ALL enabled surfaces regardless of which table/process
    created them, found "claude_websearch" (this platform) has no
    entry in _COLLECTOR_MAP, and logged "No collector for platform
    'claude_websearch'" on every single tick. Both discovery_loop() and
    search_queue_loop.py call save_items() directly — this surface never
    needs to be polled.
    """
    surface_key = _surface_key_for_pair(domain, topic)
    existing = await session.get(Surface, surface_key)
    if existing is None:
        session.add(Surface(
            key=surface_key,
            platform=source,
            enabled=False,
            poll_interval_sec=3600,
            max_items_per_run=5,
            config_json={"domain": domain, "topic": topic},
            authority_tier=authority_tier,
            source_type=source_type,
            audience_type=audience_type,
            domain_tags=[topic] if topic else [],
        ))
        await session.commit()
    return surface_key


async def _already_known(session, url: str) -> bool:
    """Layer 2b — code-level, hard gate. Never trust the prompt alone."""
    normalized = _normalize_url(url)
    result = await session.execute(
        select(CrawledItem.id).where(CrawledItem.url == normalized).limit(1)
    )
    return result.first() is not None


async def land_one_url(
    session,
    client,
    url: str,
    domain: str,
    surface_key: str,
    source: str = DISCOVERY_SOURCE,
) -> tuple[str, str | None]:
    """Fetches and lands exactly one candidate URL against an
    already-resolved surface_key. Re-checks the domain allowlist itself
    even though callers (land_candidates(), search_queue_loop.py) have
    typically already checked upstream — never trust an earlier check
    alone, the same "two layers" principle as everywhere else in this
    module.

    Returns (outcome, reason):
      ("landed", None)         — freshly inserted into crawled_items
      ("already_known", None)  — already in crawled_items (URL dedup hit,
                                  or save_items() upserted an existing row)
      ("failed", reason)       — reason is one of:
        "not_allowlisted" | "robots_blocked" | "http_404" | "http_403" |
        "fetch_failed" (timeout/connect/5xx or 429 exhausted/circuit
        breaker open — see src/http/client.py's RateLimitedClient.get())
        | "extract_failed" (fetched OK, no extractable content)

    search_queue_loop.py maps `reason` to a retry policy (websearch.txt
    15.2); land_candidates() only logs it, matching its pre-existing
    behavior for the proactive discovery_loop() path.
    """
    if not _passes_domain_allowlist(url, domain):
        return "failed", "not_allowlisted"
    if await _already_known(session, url):
        return "already_known", None

    try:
        page_resp = await client.get(url, use_browser_ua=True, check_robots=True)
    except FileNotFoundError:
        return "failed", "http_404"
    except PermissionError as exc:
        if "robots.txt" in str(exc):
            return "failed", "robots_blocked"
        return "failed", "http_403"
    except (httpx.TimeoutException, httpx.ConnectError, RuntimeError) as exc:
        logger.warning("discovery: fetch failed for %s: %s", url, exc)
        return "failed", "fetch_failed"
    except Exception as exc:
        logger.warning("discovery: unexpected fetch error for %s: %s", url, exc)
        return "failed", "fetch_failed"

    soup = BeautifulSoup(page_resp.text, "html.parser")
    item = _extract_page(soup, url)
    if not item:
        return "failed", "extract_failed"
    item["source"] = source
    inserted = await save_items([item], surface_key, session)
    if inserted:
        return "landed", None
    # save_items() is an upsert — inserted=0 can also mean a concurrent
    # path landed this exact URL between our _already_known() check above
    # and this save. Either way the content is there now; nothing failed.
    return "already_known", None


async def land_candidates(
    candidates: list[dict],
    domain: str,
    topic: str | None,
    authority_tier: int | None,
    source_type: str | None,
    audience_type: str | None,
    source: str = DISCOVERY_SOURCE,
) -> int:
    """Runs candidates through both trust/dedup layers, fetches survivors
    with the existing single-page extractor, and stores them via the
    existing pipeline.save_items() (via land_one_url() per candidate).
    Returns count actually inserted (NOT counting already-known hits —
    matches the pre-existing contract).

    topic/source default to the proactive discovery_loop() shape (topic
    required by callers, source="claude_websearch"); the search-queue
    reactive path (websearch.txt 15.1) passes topic=None and
    source="search_websearch_queue"."""
    if not candidates:
        return 0

    client = get_shared_client()
    landed_count = 0

    async with AsyncSessionLocal() as session:
        surface_key = await _ensure_discovery_surface(
            session, domain, topic, authority_tier, source_type, audience_type, source,
        )

        for cand in candidates:
            url = cand.get("url", "")
            if not url:
                continue
            outcome, reason = await land_one_url(session, client, url, domain, surface_key, source)
            if outcome == "landed":
                landed_count += 1
            elif reason == "not_allowlisted":
                logger.warning(
                    "discovery: dropping %s — not on allowlisted domain %s (claude -p prompt said site:%s but code-level check disagrees)",
                    url, domain, domain,
                )
            elif reason == "robots_blocked":
                logger.info("discovery: robots.txt blocked %s", url)

        await session.commit()

    if landed_count:
        logger.info("discovery: landed %d new item(s) for (%s, %s)", landed_count, domain, topic)
    return landed_count


async def land_search_queue_url(
    url: str,
    domain: str,
    authority_tier: int | None,
    source_type: str | None,
    audience_type: str | None,
) -> tuple[str, str | None]:
    """Entry point for the search-queue reactive path (websearch.txt
    15.1/15.5) — src/discovery/search_queue_loop.py calls this once per
    search_discovery_requests row whose domain already resolved to a
    known tier1/2 surface (queue.surface_metadata_for_domain()). Ensures
    the per-domain synthetic Surface (topic=None) exists, then lands the
    single URL through the same land_one_url() the proactive path uses.
    Owns its own session/commit — callers don't need one.

    Returns the same (outcome, reason) contract as land_one_url().
    """
    client = get_shared_client()
    async with AsyncSessionLocal() as session:
        surface_key = await _ensure_discovery_surface(
            session, domain, None, authority_tier, source_type, audience_type,
            SEARCH_QUEUE_SOURCE,
        )
        outcome, reason = await land_one_url(session, client, url, domain, surface_key, SEARCH_QUEUE_SOURCE)
        await session.commit()
    return outcome, reason
