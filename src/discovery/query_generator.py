"""Calls `claude -p` with WebSearch to find candidate URLs for one
(domain, topic) queue pair — crawl.txt section 3 "RETEST 2026-08-26" /
section 13.2-13.3.

claude -p is given ONLY the WebSearch tool (--tools WebSearch — Bash/
PowerShell/REPL/WebFetch are simply never in that list, so they're not
available) and its output is constrained by DISCOVERY_RESULTS_SCHEMA to
title+url pairs — no article body, no summary. That's the trust-boundary
line crawl.txt sections 2/4 draw at the code level: the LLM never decides
what's trustworthy or supplies content, only candidate URLs;
src/discovery/landing.py does the actual fetch and every dedup/allowlist
check.

Verified against the real CLI (2026-08-28) before writing this: --tools
WebSearch and --json-schema exist as documented (`claude -p --help`), and
--output-format json wraps the schema-validated reply in a top-level
"structured_output" object (confirmed by a live call). Also verified, the
hard way, in the FIRST live discovery_loop() run (2026-08-28 18:00:42,
pair (aap.org, adhd)): an earlier version of this command additionally
passed --restricted "for defense in depth" — that flag made the CLI
DENY its own WebSearch tool_use twice (visible in the run's
"permission_denials", not in any exit code or stderr), so the model
silently returned an empty result set every time, indistinguishable from
a real "found nothing" response. --restricted is NOT combinable with
--tools this way; --tools alone is the actual scoping mechanism and is
sufficient on its own — confirmed by rerunning the identical prompt
without --restricted, which returned 5 real aap.org ADHD URLs.
"""
from __future__ import annotations

import asyncio
import json
import logging

from src.discovery.schemas import DISCOVERY_RESULTS_SCHEMA

logger = logging.getLogger(__name__)

_CLAUDE_BIN = "claude"
# crawl.txt's 2026-08-26 retest: one domain/one topic, single site: search,
# completed in ~16s. 90s leaves generous headroom without letting one
# stuck call block the hourly cadence for long.
_TIMEOUT_SEC = 90
# Same cap the working 2026-08-26 retest used ($0.056 actual, well under
# this). A 5-domain broad ask blew through $0.20 without finishing in the
# earlier failed test — this cap exists to fail fast and cheaply if a
# call somehow reverts to that broad-ask shape instead of silently
# running up cost/time.
_MAX_BUDGET_USD = "0.20"
_MAX_RESULTS = 10


async def find_candidate_urls(domain: str, topic: str, known_urls: list[str]) -> list[dict]:
    """Returns [{"url": ..., "title": ...}, ...], newest-safe: [] on ANY
    failure (timeout, budget exhaustion, malformed output, non-zero
    exit). A discovery pass finding nothing is a normal, silent outcome
    here — this is a supplement layer (crawl.txt section 3 layer 4), not
    something worth crashing discovery_loop() over.
    """
    prompt = _build_prompt(domain, topic, known_urls)
    cmd = [
        _CLAUDE_BIN, "-p", prompt,
        "--tools", "WebSearch",
        "--output-format", "json",
        "--json-schema", json.dumps(DISCOVERY_RESULTS_SCHEMA),
        "--max-budget-usd", _MAX_BUDGET_USD,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:
        logger.error("discovery: failed to spawn claude -p for (%s, %s): %s", domain, topic, exc)
        return []

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        logger.warning("discovery: claude -p timed out (%ds) for (%s, %s)", _TIMEOUT_SEC, domain, topic)
        return []

    if proc.returncode != 0:
        logger.warning(
            "discovery: claude -p exited %d for (%s, %s): %s",
            proc.returncode, domain, topic, (stderr or b"").decode(errors="replace")[:500],
        )
        return []

    candidates = _parse_output(stdout, domain, topic)
    return candidates[:_MAX_RESULTS]


def _build_prompt(domain: str, topic: str, known_urls: list[str]) -> str:
    # Layer 1 dedup (crawl.txt section 13.3, "第一层"): list recently-known
    # URLs for this domain so the model tries to avoid resurfacing them.
    # Soft signal only — src/discovery/landing.py never trusts this on its
    # own, it re-checks crawled_items.url regardless (layer 2).
    # "Up to a few times" (not "once or twice") — still one domain, one
    # topic, well within the shape crawl.txt's 2026-08-26 retest proved
    # viable; only the search-call count and maxItems changed (2026-08-28,
    # see DISCOVERY_RESULTS_SCHEMA's comment) to use more of the real
    # measured cost headroom under --max-budget-usd.
    known_block = "\n".join(known_urls[:30]) if known_urls else "(none yet)"
    return (
        f"Use WebSearch a few times with different phrasings if needed: "
        f"site:{domain} {topic}. Return up to {_MAX_RESULTS} real result "
        f"URLs and titles for pages not already in the list below.\n\n"
        f"Already collected on {domain} — do not return these:\n{known_block}"
    )


def _parse_output(stdout: bytes, domain: str, topic: str) -> list[dict]:
    try:
        payload = json.loads(stdout.decode(errors="replace"))
    except Exception as exc:
        logger.warning("discovery: claude -p output was not valid JSON for (%s, %s): %s", domain, topic, exc)
        return []

    results = payload.get("structured_output")
    if results is None:
        # Fallback path — structured_output was confirmed present in a live
        # test run, but stay defensive against a CLI version where it isn't:
        # "result" holds the same schema-validated payload as a JSON string.
        raw = payload.get("result")
        if isinstance(raw, str):
            try:
                results = json.loads(raw)
            except Exception:
                results = None
        elif isinstance(raw, dict):
            results = raw

    if not isinstance(results, dict):
        logger.warning("discovery: no structured_output/result for (%s, %s)", domain, topic)
        return []

    candidates = results.get("results", [])
    if not isinstance(candidates, list):
        return []
    return [c for c in candidates if isinstance(c, dict) and c.get("url") and c.get("title")]
