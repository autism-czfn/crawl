"""Pure-logic tests for src/discovery/search_queue_loop.py — websearch.txt
15.1/15.2/十九. No DB: _domain_from_url and _compute_retry_update are both
pure (the latter takes `now` explicitly, same pattern as
discovery/loop.py::_startup_delay_seconds — see test_discovery_loop_pacing.py)."""
from datetime import datetime, timedelta, timezone

from src.discovery.search_queue_loop import (
    _MAX_RETRY_COUNT,
    _compute_retry_update,
    _domain_from_url,
)


def test_domain_from_url_strips_scheme_and_www():
    assert _domain_from_url("https://www.cdc.gov/sleep/about") == "cdc.gov"
    assert _domain_from_url("https://cdc.gov/sleep/about") == "cdc.gov"
    assert _domain_from_url("http://europepmc.org/article/123") == "europepmc.org"


def test_domain_from_url_unparseable_returns_none():
    assert _domain_from_url("not a url") is None
    assert _domain_from_url("") is None


def test_permanent_reasons_never_get_a_retry_time():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for reason in ("http_404", "robots_blocked", "unparseable_url", "not_allowlisted"):
        retry_count, next_retry_at = _compute_retry_update(reason, retry_count_before=0, now=now)
        assert retry_count == 1
        assert next_retry_at is None, reason


def test_http_403_retries_in_30_days():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    retry_count, next_retry_at = _compute_retry_update("http_403", retry_count_before=0, now=now)
    assert retry_count == 1
    assert next_retry_at == now + timedelta(days=30)


def test_fetch_failed_and_extract_failed_retry_in_1_day():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for reason in ("fetch_failed", "extract_failed"):
        retry_count, next_retry_at = _compute_retry_update(reason, retry_count_before=0, now=now)
        assert next_retry_at == now + timedelta(days=1), reason


def test_retry_count_reaching_max_forces_permanent_even_for_a_transient_reason():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    retry_count, next_retry_at = _compute_retry_update(
        "fetch_failed", retry_count_before=_MAX_RETRY_COUNT - 1, now=now,
    )
    assert retry_count == _MAX_RETRY_COUNT
    assert next_retry_at is None


def test_retry_count_always_increments_regardless_of_reason():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    retry_count, _ = _compute_retry_update("http_404", retry_count_before=3, now=now)
    assert retry_count == 4
