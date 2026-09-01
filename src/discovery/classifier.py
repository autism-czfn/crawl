"""Calls `claude -p` (Haiku) to classify a domain search's WebSearch
fallback found that isn't yet in config/surfaces.json as tier1/2 —
websearch.txt section 十九's "留档供人工复核" made automatic: instead of
leaving an out_of_scope row for a human who has no review surface to act
on (there isn't one — confirmed, no admin UI touches this table), ask the
model the same question a human reviewer would, and let
src/discovery/surfaces_writer.py act on a confident "yes."

Same trust-boundary line query_generator.py already draws (crawl.txt
sections 2/4): the model only ever proposes a classification, it never
writes anything itself — src/discovery/search_queue_loop.py decides
whether to act on it (confidence=="high" only), and surfaces_writer.py
does the actual (validated, atomic) file write.
"""
from __future__ import annotations

import asyncio
import json
import logging

from src.discovery.schemas import DOMAIN_CLASSIFY_SCHEMA

logger = logging.getLogger(__name__)

_CLAUDE_BIN = "claude"
_MODEL = "claude-haiku-4-5-20251001"
# A single-domain lookup is a much smaller ask than query_generator.py's
# multi-search discovery call — 60s leaves headroom without letting one
# stuck call hold up a 180s search-queue cycle for long.
_TIMEOUT_SEC = 60
# $0.05 measured too tight in practice (2026-08-31 live test): a real
# classify_domain() call that actually uses WebSearch + extended thinking
# to check who operates the domain ran $0.055 and hit budget_exhausted
# before ever returning structured_output — indistinguishable from any
# other failure (fail-open, retried next cycle) but wasted the spend for
# nothing. $0.15 leaves real headroom while staying well under
# query_generator.py's $0.20 (a bigger, multi-search-call task).
_MAX_BUDGET_USD = "0.15"


async def classify_domain(
    domain: str,
    url: str,
    title: str | None,
    snippet: str | None,
    trigger_query: str | None,
) -> dict | None:
    """Returns {"tier": 1|2|None, "source_type": ..., "audience_type": ...,
    "confidence": "high"|"medium"|"low", "reason": ...}, or None on ANY
    failure (spawn error, timeout, non-zero exit, malformed output) — same
    fail-open contract as query_generator.find_candidate_urls(). None
    means "couldn't classify, try again later," never an implicit
    approval — search_queue_loop.py only ever promotes a domain on an
    explicit tier + confidence=="high" result.
    """
    prompt = _build_prompt(domain, url, title, snippet, trigger_query)
    cmd = [
        _CLAUDE_BIN, "-p", prompt,
        "--model", _MODEL,
        "--tools", "WebSearch",
        "--output-format", "json",
        "--json-schema", json.dumps(DOMAIN_CLASSIFY_SCHEMA),
        "--max-budget-usd", _MAX_BUDGET_USD,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:
        logger.error("classifier: failed to spawn claude -p for %s: %s", domain, exc)
        return None

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        logger.warning("classifier: claude -p timed out (%ds) for %s", _TIMEOUT_SEC, domain)
        return None

    if proc.returncode != 0:
        logger.warning(
            "classifier: claude -p exited %d for %s: %s",
            proc.returncode, domain, (stderr or b"").decode(errors="replace")[:500],
        )
        return None

    return _parse_classification(stdout, domain)


def _build_prompt(
    domain: str,
    url: str,
    title: str | None,
    snippet: str | None,
    trigger_query: str | None,
) -> str:
    return (
        f"A live user query led a web search to this page:\n"
        f"  URL: {url}\n"
        f"  Title: {title or '(none)'}\n"
        f"  Snippet: {snippet or '(none)'}\n"
        f"  User's query: {trigger_query or '(unknown)'}\n\n"
        f"Is {domain} an authoritative source for autism / sleep / eating / "
        f"behavioral / ADHD health content, in the same class as sources "
        f"like cdc.gov, nhs.uk, aap.org, or a children's hospital?\n\n"
        f"tier=1: a government health agency, or a major national/"
        f"international medical or professional body that issues clinical "
        f"guidance.\n"
        f"tier=2: respected but secondary — an accredited hospital, "
        f"academic medical center, recognized nonprofit health "
        f"organization, or peer-reviewed academic publisher.\n"
        f"tier=null: anything else (personal blog, forum, commercial "
        f"content site, local news, an operator you can't verify, etc.) "
        f"— when in doubt, prefer null over guessing.\n\n"
        f"Use WebSearch to check who actually operates {domain} before "
        f"deciding — don't guess from the URL alone. Only answer "
        f"confidence=\"high\" when you're certain; use \"medium\" or "
        f"\"low\" for anything you're not fully sure of — a low-confidence "
        f"tier is treated the same as a reject, so there's no cost to "
        f"admitting uncertainty."
    )


def _parse_classification(stdout: bytes, domain: str) -> dict | None:
    try:
        payload = json.loads(stdout.decode(errors="replace"))
    except Exception as exc:
        logger.warning("classifier: claude -p output was not valid JSON for %s: %s", domain, exc)
        return None

    result = payload.get("structured_output")
    if result is None:
        # Fallback path — same defensive shape as query_generator.py's
        # _parse_output: "result" holds the same schema-validated payload
        # as a JSON string on some CLI versions.
        raw = payload.get("result")
        if isinstance(raw, str):
            try:
                result = json.loads(raw)
            except Exception:
                result = None
        elif isinstance(raw, dict):
            result = raw

    if not isinstance(result, dict):
        logger.warning("classifier: no structured_output/result for %s", domain)
        return None

    tier = result.get("tier")
    confidence = result.get("confidence")
    reason = result.get("reason")
    if (
        tier not in (1, 2, None)
        or confidence not in ("high", "medium", "low")
        or not isinstance(reason, str)
        or not reason
    ):
        logger.warning("classifier: malformed classification for %s: %r", domain, result)
        return None

    return {
        "tier": tier,
        "source_type": result.get("source_type"),
        "audience_type": result.get("audience_type"),
        "confidence": confidence,
        "reason": reason,
    }
