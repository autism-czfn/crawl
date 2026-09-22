from __future__ import annotations

import asyncio
import hashlib
import logging
import socket
import time
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from src.config import settings
from src.http.jitter import exponential_backoff

logger = logging.getLogger(__name__)


def _is_dns_failure(exc: BaseException) -> bool:
    """True if a connection failure was specifically DNS resolution
    (getaddrinfo) failing to resolve the hostname at all, as opposed to a
    reachable host timing out, refusing the connection, or failing a TLS
    handshake. Distinguishing this matters: a burst of DNS failures across
    many unrelated domains at once means OUR network/DNS resolver is
    having a problem, not that a bunch of different sites all went down —
    that distinction determines what's worth investigating.

    Walks the exception's __cause__ chain for a socket.gaierror — what
    CPython's socket module raises for getaddrinfo failures on every OS —
    with a string-match fallback in case some layer of httpx/anyio wraps
    it without preserving that chain.
    """
    cause: BaseException | None = exc
    for _ in range(5):
        if isinstance(cause, socket.gaierror):
            return True
        cause = getattr(cause, "__cause__", None)
        if cause is None:
            break
    msg = str(exc)
    return "nodename nor servname" in msg or "Name or service not known" in msg or "Temporary failure in name resolution" in msg


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

    def update_rate(self, new_rate: float) -> None:
        """Adjust the refill rate live (e.g. from a server-advertised rate
        limit header) without resetting current token count/timing."""
        self.rate = new_rate
        self.capacity = max(new_rate * 5, 1)

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
    # Safety net for the half-open probe slot below: if whichever caller won
    # it never reports back via record_success/record_failure (e.g. it hit
    # an exception type the retry loop in request() doesn't catch, so
    # neither gets called), don't let the breaker wedge shut forever — treat
    # the slot as abandoned after this long and let a fresh probe through.
    # Comfortably above request()'s own per-attempt timeout.
    half_open_probe_timeout_sec = 60

    def __init__(self, domain: str = "") -> None:
        self.domain = domain
        self.state = "closed"
        self.failures = 0
        self.opened_at: float | None = None
        self._half_open_probe_started_at: float | None = None

    def record_success(self) -> None:
        self.failures = 0
        self.state = "closed"
        self._half_open_probe_started_at = None

    def record_failure(self) -> None:
        self.failures += 1
        self._half_open_probe_started_at = None
        if self.state == "open":
            # Already open and cooling down — this is a trailing failure
            # from a call that started (and passed allow_request()) before
            # the breaker tripped, e.g. one of a domain's other concurrent
            # in-flight requests each independently retrying up to 3x. It
            # doesn't mean the breaker "reopened"; don't push opened_at (and
            # therefore the 5-minute cooldown) further out for it, and don't
            # re-log — confirmed live 2026-09-10: doi.org logged "OPEN"
            # twice 2s apart, nowhere near the real 300s cooldown, from
            # exactly this.
            return
        if self.failures >= self.threshold:
            self.state = "open"
            self.opened_at = time.monotonic()
            logger.warning("Circuit breaker OPEN for %s", self.domain or "?")

    def allow_request(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if self.opened_at and time.monotonic() - self.opened_at > self.cooldown_sec:
                self.state = "half_open"
            else:
                return False
        # half_open: let exactly ONE probe through at a time, tracked by a
        # timestamp rather than a plain bool so a probe whose outcome never
        # gets recorded doesn't wedge this domain shut permanently (see
        # half_open_probe_timeout_sec above).
        #
        # Bug this replaces (found 2026-09-10, from crawler.log showing
        # 95k+ "Circuit breaker OPEN" lines over ~2 weeks, dominated by
        # doi.org): the old code returned True unconditionally for every
        # caller while state == "half_open", with no notion of a probe
        # already being in flight. Every task waiting on this domain
        # (concurrency is 3 by default) would see the elapsed cooldown at
        # once and ALL rush doi.org simultaneously instead of one canary
        # request checking first. Since `failures` is never reset except on
        # success, a single failure among that herd immediately reopens the
        # breaker for another 5 minutes — and sending 3 requests instead of
        # 1 into a still-fragile target raises the odds that at least one
        # of them fails (1-0.8^3 ≈ 49% vs. 20% for a lone probe at 80%
        # success), so recovery got harder to observe, not easier, the more
        # concurrency a domain had.
        now = time.monotonic()
        if (
            self._half_open_probe_started_at is not None
            and now - self._half_open_probe_started_at < self.half_open_probe_timeout_sec
        ):
            return False
        self._half_open_probe_started_at = now
        return True


# ---------------------------------------------------------------------------
# Default rate limits per domain (rpm)
# ---------------------------------------------------------------------------

DOMAIN_RATE_LIMITS: dict[str, float] = {
    "reddit.com": 60,
    "pubmed.ncbi.nlm.nih.gov": 10,  # human-readable article pages (enrich_fulltext)
    "eutils.ncbi.nlm.nih.gov": 180,  # the ACTUAL PubMed API domain — was missing
    # (pubmed.py calls eutils.ncbi.nlm.nih.gov, not pubmed.ncbi.nlm.nih.gov;
    # without this entry every real esearch/efetch call silently fell back
    # to DEFAULT_RATE_LIMIT). 180rpm = NCBI's own documented no-API-key limit
    # (3 req/sec, confirmed 2026-08-27 via https://www.ncbi.nlm.nih.gov/books/NBK25497/
    # — "no more than three URL requests per second"). Was set to 10rpm before,
    # 18x under NCBI's own stated ceiling. A free NCBI API key (PUBMED_API_KEY
    # in .env, currently empty, already wired up in pubmed.py) would raise
    # NCBI's own limit further to 10 req/sec (600rpm) if one is added later.
    "ebi.ac.uk": 10,
    "api.semanticscholar.org": 20,
    "api.crossref.org": 50,
    "api.biorxiv.org": 10,
    "doaj.org": 2,
    "en.wikipedia.org": 60,
    "api.openalex.org": 60,
    "api.unpaywall.org": 60,  # was missing entirely (silently on the 20rpm
    # default) despite Unpaywall's own published guidance tolerating far
    # more (~100k requests/day ≈ 69rpm sustained) — 60 stays conservatively
    # under that ceiling.
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


def _parse_rate_limit_headers(headers: httpx.Headers) -> float | None:
    """Parse Crossref-style X-Rate-Limit-Limit / X-Rate-Limit-Interval
    response headers into a requests-per-second rate.

    Crossref deliberately does NOT publish a fixed rate limit — their docs
    state it varies over time and must be read from these two headers on
    every response instead (confirmed 2026-08-27 via
    https://github.com/CrossRef/rest-api-doc#etiquette). Other providers we
    talk to don't send these headers, so this only ever fires for domains
    that actually advertise it — everyone else keeps their static
    DOMAIN_RATE_LIMITS entry untouched.

    Interval is documented as e.g. "1s" — a number followed by a unit
    letter (s=seconds, m=minutes, h=hours). Returns None if the headers are
    absent or don't parse cleanly (never raises — a malformed/unexpected
    header value should never break a real request).
    """
    limit = headers.get("X-Rate-Limit-Limit")
    interval = headers.get("X-Rate-Limit-Interval")
    if not limit or not interval:
        return None
    try:
        limit_val = float(limit)
        unit_seconds = {"s": 1, "m": 60, "h": 3600}.get(interval[-1].lower())
        if unit_seconds is None:
            interval_val = float(interval)  # no unit suffix — assume seconds
        else:
            interval_val = float(interval[:-1]) * unit_seconds
        if interval_val <= 0:
            return None
        return limit_val / interval_val
    except (ValueError, IndexError):
        return None


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
        self._server_advertised_rps: dict[str, float] = {}  # last value applied, to log only on change
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            # A flat 30s applied to connect AND read let one stuck TCP/TLS
            # handshake hold a domain's concurrency slot (only 3 by default)
            # for 30s per failed attempt, up to 3 attempts — a single
            # unreachable item could tie up a slot for ~90s+backoff, and with
            # the domain's other queued items stuck behind it, dragged a
            # batch's tail out to 20+ minutes. Confirmed live 2026-09-10:
            # doi.org repeatedly stalling on a handful of DOIs individually,
            # each retried 3x at up to 30s/attempt, kept the SAME few items
            # occupying doi.org's 3 concurrency slots across many progress
            # checkpoints spanning ~20 minutes. Tightening connect (a real
            # unreachable host fails to even open a socket almost
            # immediately, never near 30s — every manual check here
            # connected in <100ms) while leaving read/write generous means a
            # genuinely slow-but-working download (e.g. a large PDF) is
            # unaffected, but a stalled/unreachable connect attempt frees
            # the slot in a third of the time.
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0),
            # httpx defaults to a 100-connection GLOBAL pool cap (all domains
            # combined) when this isn't set — completely separate from, and
            # far tighter than, the per-domain semaphores above (capped at 3
            # each). That's invisible right up until a batch spans enough
            # distinct domains to exceed 100 in flight at once: everything
            # past the 100th queues for a pool slot and, if it doesn't get
            # one within the 30s timeout above, fails with a PoolTimeout —
            # which looks IDENTICAL to a real network timeout in the log
            # (caught by the same `except httpx.TimeoutException` below) even
            # though the actual target site was never even contacted.
            # Confirmed live 2026-09-10: raising enrich_fulltext's batch_size
            # 300→500 pushed a batch over this cap and produced a burst of
            # simultaneous "Timeout/connect error" across totally unrelated
            # domains (doi.org, dx.doi.org, half a dozen .ekb.eg journals at
            # once) — while doi.org itself responded in <100ms to a direct
            # curl run seconds later. This host's own fd ulimit is 1M+, so
            # there's no real ceiling forcing 100 here.
            limits=httpx.Limits(max_connections=300, max_keepalive_connections=50),
        )

    def _apply_server_rate_limit(self, domain: str, resp: httpx.Response) -> None:
        """If this response carries Crossref-style rate-limit headers, adapt
        this domain's TokenBucket to it live instead of relying on the
        static DOMAIN_RATE_LIMITS guess (see _parse_rate_limit_headers)."""
        new_rps = _parse_rate_limit_headers(resp.headers)
        if new_rps is None:
            return
        if self._server_advertised_rps.get(domain) == new_rps:
            return  # unchanged since last time — nothing to do or log
        self._server_advertised_rps[domain] = new_rps
        self._bucket(domain).update_rate(new_rps)
        logger.info(
            "%s advertised its own rate limit via response headers: %.2f req/s (%.0f rpm) — applied live",
            domain, new_rps, new_rps * 60,
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
            self._breakers[domain] = CircuitBreaker(domain)
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
                    # Applied against the REQUESTED domain's bucket (`domain`),
                    # since that's what acquire() actually throttles against —
                    # not final_domain, which for a redirecting request (e.g.
                    # doi.org) is a different server that never saw our token
                    # bucket at all.
                    self._apply_server_rate_limit(domain, resp)

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
                    if _is_dns_failure(exc):
                        # Distinct, greppable line (with the full URL, not
                        # just the domain) — added 2026-09-11 after a ~2h
                        # stretch where every domain failed with DNS errors
                        # at once (local network issue, not any one site)
                        # and the generic "Timeout/connect error" wording
                        # made that hard to tell apart from a real per-site
                        # timeout without reading each exception message.
                        logger.warning(
                            "DNS resolution failed for %s (domain %s) — retrying in %.1fs: %s",
                            url, domain, wait, exc,
                        )
                    else:
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
