from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from src.config import settings
from src.http.jitter import exponential_backoff

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token Bucket (O3 — option B)
# ---------------------------------------------------------------------------

class TokenBucket:
    def __init__(self, rate: float, capacity: float):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now
            if self.tokens >= 1:
                self.tokens -= 1
            else:
                wait = (1 - self.tokens) / self.rate
                self.tokens = 0
                await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# Circuit Breaker (Strategy 4)
# ---------------------------------------------------------------------------

class CircuitBreaker:
    threshold = 5
    cooldown_sec = 300

    def __init__(self) -> None:
        self.state = "closed"
        self.failures = 0
        self.opened_at: float | None = None

    def record_success(self) -> None:
        self.failures = 0
        self.state = "closed"

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.state = "open"
            self.opened_at = time.monotonic()
            logger.warning("Circuit breaker OPEN")

    def allow_request(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if self.opened_at and time.monotonic() - self.opened_at > self.cooldown_sec:
                self.state = "half_open"
                return True
            return False
        return True  # half_open: allow one probe


# ---------------------------------------------------------------------------
# Default rate limits per domain (rpm)
# ---------------------------------------------------------------------------

DOMAIN_RATE_LIMITS: dict[str, float] = {
    "reddit.com": 60,
    "pubmed.ncbi.nlm.nih.gov": 10,  # human-readable article pages (enrich_fulltext)
    "eutils.ncbi.nlm.nih.gov": 10,  # the ACTUAL PubMed API domain — was missing
    # (pubmed.py calls eutils.ncbi.nlm.nih.gov, not pubmed.ncbi.nlm.nih.gov;
    # without this entry every real esearch/efetch call silently fell back
    # to DEFAULT_RATE_LIMIT instead of the intended 10 rpm)
    "ebi.ac.uk": 10,
    "api.semanticscholar.org": 20,
    "api.crossref.org": 50,
    "api.biorxiv.org": 10,
    "doaj.org": 2,
    "en.wikipedia.org": 60,
    "api.openalex.org": 60,
    "clinicaltrials.gov": 20,
    "api.core.ac.uk": 10,
    "newsapi.org": 0.07,  # free tier: 100 req/day ≈ 0.07 rpm
    "youtube.googleapis.com": 60,
    "wrongplanet.net": 6,
}
DEFAULT_RATE_LIMIT = 20  # rpm


# ---------------------------------------------------------------------------
# Per-domain concurrency (Action item A1 — 2026-08-26)
# ---------------------------------------------------------------------------
# Every domain used to share ONE concurrency value (3) regardless of type.
# Academic APIs are built for automated/parallel access and document their
# own rate limits above (which still cap total throughput via the token
# bucket) — there's no reason to also cap them at the same concurrency as a
# human-facing HTML site sitting behind CDN/anti-bot protection. Playwright
# stays separately capped even lower at the scheduler level (see
# src/scheduler.py _PLAYWRIGHT_CONCURRENCY). doi.org is deliberately NOT
# listed here despite being hit by every academic collector — it's a shared
# redirect front-door for every publisher's DOIs, and empirically 429s us
# aggressively on its own; raising its concurrency would make that worse,
# not better.
DOMAIN_CONCURRENCY: dict[str, int] = {
    "eutils.ncbi.nlm.nih.gov": 8,
    "api.crossref.org": 8,
    "api.openalex.org": 8,
    "ebi.ac.uk": 8,
    "api.semanticscholar.org": 6,
    "api.core.ac.uk": 6,
    "api.biorxiv.org": 6,
    "clinicaltrials.gov": 6,
    "doaj.org": 3,  # explicitly rate-limited to 2 rpm above — concurrency wouldn't help
}
DEFAULT_CONCURRENCY = 3  # unchanged default for HTML/Playwright/everything else


def _domain(url: str) -> str:
    netloc = urlparse(url).netloc
    return netloc[4:] if netloc.startswith("www.") else netloc


def _rpm_to_rps(rpm: float) -> float:
    return rpm / 60.0


# ---------------------------------------------------------------------------
# robots.txt cache
# ---------------------------------------------------------------------------

_robots_cache: dict[str, tuple[RobotFileParser, float]] = {}
_ROBOTS_TTL = 86400  # 24 hours


async def _is_allowed(url: str, client: httpx.AsyncClient) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    now = time.monotonic()

    cached = _robots_cache.get(parsed.netloc)
    if cached and now - cached[1] < _ROBOTS_TTL:
        rp = cached[0]
    else:
        rp = RobotFileParser()
        rp.set_url(robots_url)
        try:
            resp = await client.get(robots_url, timeout=10)
            rp.parse(resp.text.splitlines())
        except Exception:
            return True  # if robots.txt unreachable, allow
        _robots_cache[parsed.netloc] = (rp, now)

    return rp.can_fetch(settings.USER_AGENT, url)


# ---------------------------------------------------------------------------
# RateLimitedClient
# ---------------------------------------------------------------------------

class RateLimitedClient:
    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
        )

    def _bucket(self, domain: str) -> TokenBucket:
        if domain not in self._buckets:
            rpm = DOMAIN_RATE_LIMITS.get(domain, DEFAULT_RATE_LIMIT)
            rps = _rpm_to_rps(rpm)
            self._buckets[domain] = TokenBucket(rate=rps, capacity=max(rps * 5, 1))
        return self._buckets[domain]

    def _semaphore(self, domain: str) -> asyncio.Semaphore:
        if domain not in self._semaphores:
            limit = DOMAIN_CONCURRENCY.get(domain, DEFAULT_CONCURRENCY)
            self._semaphores[domain] = asyncio.Semaphore(limit)
        return self._semaphores[domain]

    def _breaker(self, domain: str) -> CircuitBreaker:
        if domain not in self._breakers:
            self._breakers[domain] = CircuitBreaker()
        return self._breakers[domain]

    async def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        use_browser_ua: bool = False,
        check_robots: bool = False,
    ) -> httpx.Response:
        domain = _domain(url)
        breaker = self._breaker(domain)

        if not breaker.allow_request():
            raise RuntimeError(f"Circuit breaker OPEN for {domain}")

        if check_robots and not await _is_allowed(url, self._client):
            raise PermissionError(f"robots.txt disallows {url}")

        base_headers = {
            "User-Agent": settings.BROWSER_USER_AGENT if use_browser_ua else settings.USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
        if headers:
            base_headers.update(headers)

        async with self._semaphore(domain):
            await self._bucket(domain).acquire()

            for attempt in range(3):
                try:
                    resp = await self._client.get(url, headers=base_headers, params=params)

                    # Attribute the OUTCOME (breaker success/failure) to the
                    # FINAL domain after any redirects, not the one we
                    # originally requested — e.g. doi.org routinely redirects
                    # to the real publisher, and it's whoever we actually
                    # landed on that succeeded or blocked us, not doi.org
                    # itself. The pre-request checks above (allow_request,
                    # rate bucket, concurrency semaphore) unavoidably still
                    # key on the requested domain — we don't know the
                    # destination until after the request completes — but
                    # that's fine: those are about not hammering the front
                    # door itself, which is a legitimate concern regardless
                    # of where it redirects.
                    final_domain = _domain(str(resp.url))
                    outcome_breaker = self._breaker(final_domain) if final_domain != domain else breaker

                    if resp.status_code in range(200, 300) or resp.status_code == 304:
                        outcome_breaker.record_success()
                        return resp

                    if resp.status_code == 429:
                        retry_after = float(resp.headers.get("Retry-After", exponential_backoff(attempt)))
                        logger.warning("429 on %s — waiting %.1fs", final_domain, retry_after)
                        await asyncio.sleep(retry_after)
                        continue

                    if resp.status_code == 403:
                        outcome_breaker.record_failure()
                        logger.error("403 BLOCKED on %s", final_domain)
                        # resp.url is the FINAL URL after any redirects (e.g.
                        # doi.org -> the actual publisher) — attach it so
                        # callers that attribute blame per-domain (see
                        # src/pipeline.py's blocked_domains tracking) can
                        # blame the site that actually 403'd, not the
                        # redirector that merely forwarded the request there.
                        exc = PermissionError(f"403 on {url}")
                        exc.final_url = str(resp.url)
                        raise exc

                    if resp.status_code == 404:
                        raise FileNotFoundError(f"404 on {url}")

                    if resp.status_code >= 500:
                        wait = exponential_backoff(attempt)
                        logger.warning("%d on %s — retrying in %.1fs", resp.status_code, final_domain, wait)
                        await asyncio.sleep(wait)
                        continue

                    return resp

                except (httpx.TimeoutException, httpx.ConnectError) as exc:
                    breaker.record_failure()
                    if attempt == 2:
                        raise
                    wait = exponential_backoff(attempt)
                    logger.warning("Timeout/connect error on %s — retrying in %.1fs: %s", domain, wait, exc)
                    await asyncio.sleep(wait)

            breaker.record_failure()
            raise RuntimeError(f"All retries exhausted for {url}")

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "RateLimitedClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


_shared_client: "RateLimitedClient | None" = None


def get_shared_client() -> "RateLimitedClient":
    global _shared_client
    if _shared_client is None:
        _shared_client = RateLimitedClient()
    return _shared_client
