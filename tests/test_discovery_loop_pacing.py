"""Restart-safe pacing for discovery_loop() — see _startup_delay_seconds's
docstring for the live bug this guards against (a claude -p call 9 minutes
after the previous one, when the design calls for 1 hour, because a
restart used to ignore how recently the loop had actually run)."""
from datetime import datetime, timedelta, timezone

from src.discovery.loop import _startup_delay_seconds, _INTERVAL_SEC


def test_no_prior_run_means_no_delay():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _startup_delay_seconds(None, now) == 0.0


def test_recent_restart_waits_out_the_remainder_of_the_interval():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    last_run_at = now - timedelta(minutes=9)  # exactly today's live bug
    delay = _startup_delay_seconds(last_run_at, now, interval_sec=3600)
    assert delay == 3600 - 9 * 60


def test_last_run_over_an_hour_ago_means_no_delay():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    last_run_at = now - timedelta(hours=2)
    assert _startup_delay_seconds(last_run_at, now, interval_sec=3600) == 0.0


def test_last_run_exactly_one_interval_ago_means_no_delay():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    last_run_at = now - timedelta(seconds=3600)
    assert _startup_delay_seconds(last_run_at, now, interval_sec=3600) == 0.0


def test_default_interval_matches_module_constant():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    last_run_at = now - timedelta(minutes=1)
    assert _startup_delay_seconds(last_run_at, now) == _INTERVAL_SEC - 60
