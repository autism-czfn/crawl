"""Appends a classifier-approved domain to config/surfaces.json — the
auto-promotion half of websearch.txt section 十九's "留档供人工复核" made
automatic (see src/discovery/classifier.py). Conservative by design:
config/surfaces.json has no automated validation gate (test_surfaces_config.py
only runs when someone remembers to run `setup.sh` option 9) and
src/discovery/queue.py's loaders use bare surface["key"] indexing — a
broken write here would silently disable BOTH discovery loops repo-wide
until a human noticed and fixed the file. So this module validates before
writing, dedupes against what's already there, and writes atomically
(temp file + os.replace) so a concurrent reader never sees a
half-written file.

No existing precedent in this repo for one asyncio loop writing a file
another loop reads (the closest analogs — blocked_domains,
discovery_queue_state — are Postgres tables specifically because file
writes have no durability/atomicity story otherwise); this module is
that missing precedent, kept as narrow and defensive as possible.

New entries are added with enabled=1 (2026-09-01, user decision — see
build_auto_surface_entry()'s docstring): a classifier-approved domain
joins src/scheduler.py's proactive full-site crawl rotation immediately,
same as any hand-added surface, with no human step in between.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from src.discovery.queue import _DEFAULT_SURFACES_PATH, _extract_domain

logger = logging.getLogger(__name__)


def build_auto_surface_entry(
    domain: str,
    tier: int,
    source_type: str | None,
    audience_type: str | None,
    reason: str,
    added_on: str,
) -> dict:
    """added_on is a plain ISO date string (caller passes
    datetime.now(tz=utc).date().isoformat()) — kept as a plain string
    argument rather than computed here so this function stays pure and
    trivially testable, same reasoning as discovery/loop.py's explicit
    `now` params.

    enabled=1 — full-site polling starts immediately on approval, no
    human review step (explicit user decision, 2026-09-01, overriding
    this feature's original enabled=0 default). The classifier's own
    confidence=="high" gate (search_queue_loop.py's
    _MIN_CONFIDENCE_TO_PROMOTE) plus the daily promotion cap
    (_MAX_AUTO_PROMOTIONS_PER_DAY) are the only safety valves left on
    this path now."""
    return {
        "key": f"auto_{domain.replace('.', '_')}",
        "enabled": 1,
        "authority_tier": tier,
        "platform": "html_crawl",
        "poll_interval_sec": 86400,
        "max_items": 15,
        "config": {"base_url": f"https://{domain}/"},
        "source_type": source_type,
        "audience_type": audience_type,
        "content_notes": (
            f"content: AUTO-ADDED {added_on} by websearch classifier (Haiku) — "
            f"confidence=high, reason={reason!r}; enabled=1, full-site "
            f"polling starts immediately, no human review step."
        ),
    }


def append_surface_entry(entry: dict, surfaces_path: Path | str = _DEFAULT_SURFACES_PATH) -> bool:
    """Validates, dedupes, and atomically appends `entry` to surfaces.json.
    Returns True if written, False if rejected (already covered, invalid
    shape, or the file itself couldn't be read/parsed) — never raises,
    matching every other discovery-module function's fail-safe contract.
    Synchronous (plain file I/O) — callers on the event loop should wrap
    this in asyncio.to_thread(), same as any other blocking call here."""
    surfaces_path = Path(surfaces_path)

    if not _is_valid_entry(entry):
        logger.warning("surfaces_writer: rejecting malformed entry: %r", entry)
        return False

    try:
        text = surfaces_path.read_text()
        surfaces = json.loads(text)
    except Exception as exc:
        logger.error(
            "surfaces_writer: could not read/parse %s, refusing to write: %s", surfaces_path, exc,
        )
        return False

    existing_keys = {s.get("key") for s in surfaces}
    if entry["key"] in existing_keys:
        logger.info("surfaces_writer: key %s already exists, skipping", entry["key"])
        return False

    new_domain = _extract_domain(entry)
    for s in surfaces:
        if s.get("authority_tier") in (1, 2) and _extract_domain(s) == new_domain:
            logger.info(
                "surfaces_writer: domain %s already covered by surface %s, skipping",
                new_domain, s.get("key"),
            )
            return False

    return _atomic_append(text, entry, surfaces_path)


def _is_valid_entry(entry: dict) -> bool:
    if not entry.get("key") or not isinstance(entry["key"], str):
        return False
    if entry.get("authority_tier") not in (1, 2):
        return False
    if entry.get("platform") != "html_crawl":
        return False
    if not (entry.get("config") or {}).get("base_url"):
        return False
    return True


def _atomic_append(current_text: str, entry: dict, surfaces_path: Path) -> bool:
    """Text-level append that preserves the file's existing one-entry-
    per-line compact-JSON style — NOT json.dump(..., indent=2) on the
    whole reparsed list, which would reformat every other entry too and
    turn a one-line addition into a diff nobody could review."""
    body = current_text.rstrip()
    if not body.endswith("]"):
        logger.error("surfaces_writer: %s doesn't end with ']', refusing to write", surfaces_path)
        return False
    body = body[:-1].rstrip()
    if not (body.endswith("}") or body.endswith("[")):
        logger.error("surfaces_writer: %s has unexpected structure, refusing to write", surfaces_path)
        return False

    separator = ",\n" if body.endswith("}") else "\n"
    # json.dumps()'s default separators (", ", ": ") already match the
    # existing file's one-line-per-entry style — see setup.sh/this
    # module's docstring; no indent= needed since each entry is one line.
    new_line = "  " + json.dumps(entry)
    new_text = body + separator + new_line + "\n]\n"

    try:
        json.loads(new_text)  # last-resort sanity check before it ever touches disk
    except Exception as exc:
        logger.error("surfaces_writer: generated text failed to re-parse, refusing to write: %s", exc)
        return False

    fd, tmp_path = tempfile.mkstemp(dir=surfaces_path.parent, prefix=".surfaces_", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new_text)
        os.replace(tmp_path, surfaces_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    logger.info("surfaces_writer: appended %s to %s", entry["key"], surfaces_path)
    return True
