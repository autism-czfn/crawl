"""Caption enrichment: fetches YouTube auto-generated subtitles via yt-dlp.

Schedule: runs every 10 minutes, processes up to 20 videos per batch.
Backfills content_body for YouTube items that have no captions yet.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update

from src.storage.db import AsyncSessionLocal
from src.storage.models import CrawledItem

logger = logging.getLogger(__name__)

_BATCH_SIZE = 10
_INTERVAL_SEC = 600  # 10 minutes
_DELAY_BETWEEN_VIDEOS = 10  # seconds — avoid YouTube 429 rate limits
_RETRY_STALE_DAYS = 7       # retry empty-caption videos after this many days
_RETRY_STALE_LIMIT = 20     # max rows to reset per cycle


def _fetch_subtitles(video_url: str) -> str | None:
    """Use yt-dlp to download auto-generated subtitles. Returns caption text or None.

    Uses the android player client to bypass PO Token requirements for subtitles.
    Falls back to manual subs if auto-subs are unavailable.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        out_template = str(Path(tmpdir) / "sub")
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-auto-sub",
            "--write-subs",              # also try manual subs as fallback
            "--sub-lang", "en",
            "--sub-format", "vtt",
            "--convert-subs", "srt",
            "--output", out_template,
            "--no-playlist",
            "--quiet",
            "--no-warnings",
            "--extractor-args", "youtube:player_client=android",
            video_url,
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                stderr = result.stderr[:300]
                if "429" in stderr:
                    logger.warning("yt-dlp rate-limited (429) for %s", video_url)
                    return "RATE_LIMITED"  # sentinel — caller will pause and retry
                logger.debug("yt-dlp failed for %s: %s", video_url, stderr)
                return None
        except subprocess.TimeoutExpired:
            logger.debug("yt-dlp timed out for %s", video_url)
            return None
        except FileNotFoundError:
            logger.error("yt-dlp not installed — caption enrichment disabled")
            return None

        # Find the .srt file (yt-dlp appends lang suffix)
        srt_files = list(Path(tmpdir).glob("*.srt"))
        if not srt_files:
            # No subtitles available for this video
            return None

        raw = srt_files[0].read_text(encoding="utf-8", errors="replace")
        return _srt_to_text(raw)


def _srt_to_text(srt: str) -> str:
    """Strip SRT timing/numbering, return plain text."""
    # Remove sequence numbers (lines that are just digits)
    lines = srt.strip().splitlines()
    text_lines: list[str] = []
    for line in lines:
        line = line.strip()
        # Skip blank lines, sequence numbers, and timestamp lines
        if not line:
            continue
        if re.match(r"^\d+$", line):
            continue
        if re.match(r"\d{2}:\d{2}:\d{2}", line):
            continue
        # Strip HTML-like tags from auto-generated subs
        line = re.sub(r"<[^>]+>", "", line)
        if line:
            text_lines.append(line)

    # Deduplicate consecutive identical lines (common in auto-subs)
    deduped: list[str] = []
    for line in text_lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)

    return " ".join(deduped)


async def run_once(batch_size: int = _BATCH_SIZE) -> int:
    """Fetch captions for YouTube items missing content_body. Returns count enriched."""
    enriched = 0

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(CrawledItem.id, CrawledItem.url)
            .where(CrawledItem.source == "youtube")
            .where(CrawledItem.content_body.is_(None))
            .order_by(CrawledItem.collected_at.desc())
            .limit(batch_size)
        )
        rows = result.fetchall()

    if not rows:
        return 0

    logger.info("Caption enrichment: %d YouTube videos to process", len(rows))

    for row_id, url in rows:
        # yt-dlp is synchronous and slow — run in thread
        caption_text = await asyncio.to_thread(_fetch_subtitles, url)

        if caption_text == "RATE_LIMITED":
            # Back off and stop this batch — resume next cycle
            logger.warning("Rate-limited — pausing caption enrichment for this batch")
            await asyncio.sleep(60)
            break

        if caption_text:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(CrawledItem)
                    .where(CrawledItem.id == row_id)
                    .values(content_body=caption_text)
                )
                await session.commit()
            enriched += 1
            logger.info("Caption fetched for %s (%d chars)", url, len(caption_text))
        else:
            # Mark as empty string so we don't retry forever
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(CrawledItem)
                    .where(CrawledItem.id == row_id)
                    .values(content_body="")
                )
                await session.commit()
            logger.debug("No captions available for %s", url)

        # Polite delay between videos to avoid rate limiting
        await asyncio.sleep(_DELAY_BETWEEN_VIDEOS)

    return enriched


async def _retry_stale() -> int:
    """Reset content_body to NULL for old empty-caption rows so they get retried.

    Runs once per loop cycle but only resets rows older than _RETRY_STALE_DAYS.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=_RETRY_STALE_DAYS)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(CrawledItem.id)
            .where(CrawledItem.source == "youtube")
            .where(CrawledItem.content_body == "")
            .where(CrawledItem.collected_at < cutoff)
            .limit(_RETRY_STALE_LIMIT)
        )
        ids = [r.id for r in result.fetchall()]

        if not ids:
            return 0

        await session.execute(
            update(CrawledItem)
            .where(CrawledItem.id.in_(ids))
            .values(content_body=None)
        )
        await session.commit()

    logger.info("Caption retry: reset %d stale empty-caption rows for re-processing", len(ids))
    return len(ids)


async def run_loop() -> None:
    """Long-running loop that calls run_once every 10 minutes."""
    logger.info("Caption enrichment loop started (interval=%ds)", _INTERVAL_SEC)
    cycle = 0
    while True:
        try:
            # Retry stale empty-caption rows once every 6 cycles (~1 hour)
            cycle += 1
            if cycle % 6 == 0:
                await _retry_stale()

            count = await run_once()
            if count:
                logger.info("Caption enrichment run complete: %d videos enriched", count)
        except Exception as exc:
            logger.error("Caption enrichment loop error: %s", exc)
        await asyncio.sleep(_INTERVAL_SEC)
