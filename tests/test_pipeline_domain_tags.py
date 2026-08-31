"""Tests for src/pipeline.py's _merge_tag_list — the multi-domain tag union
used so an item matched by more than one surface (e.g. an autism+ADHD
comorbidity paper hit by both pubmed_autism and pubmed_adhd) keeps every
domain it belongs to instead of the second upsert overwriting the first.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.pipeline import _merge_tag_list


def _session_returning(existing_value):
    """Build a fake AsyncSession whose .execute().first() returns a single
    row-like tuple containing `existing_value` (or no row at all if None)."""
    session = MagicMock()
    result = MagicMock()
    if existing_value is None:
        result.first.return_value = None
    else:
        result.first.return_value = (existing_value,)
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.mark.asyncio
async def test_no_existing_row_returns_sorted_new_tags():
    session = _session_returning(None)
    result = await _merge_tag_list(session, "domain_tags", "https://example.com/a", ["adhd"])
    assert result == ["adhd"]


@pytest.mark.asyncio
async def test_unions_with_existing_row_instead_of_overwriting():
    """The core regression this feature exists to prevent: pubmed_autism
    inserts domain_tags=["autism"]; pubmed_adhd later upserts the same URL
    with domain_tags=["adhd"] — the stored value must become the union, not
    just ["adhd"]."""
    session = _session_returning(["autism"])
    result = await _merge_tag_list(session, "domain_tags", "https://example.com/a", ["adhd"])
    assert result == ["adhd", "autism"]


@pytest.mark.asyncio
async def test_duplicate_tag_is_not_repeated():
    session = _session_returning(["autism", "adhd"])
    result = await _merge_tag_list(session, "domain_tags", "https://example.com/a", ["adhd"])
    assert result == ["adhd", "autism"]


@pytest.mark.asyncio
async def test_no_new_tags_keeps_existing():
    session = _session_returning(["autism"])
    result = await _merge_tag_list(session, "domain_tags", "https://example.com/a", None)
    assert result == ["autism"]


@pytest.mark.asyncio
async def test_empty_url_skips_lookup_and_returns_new_tags_only():
    session = _session_returning(["autism"])
    result = await _merge_tag_list(session, "domain_tags", "", ["adhd"])
    assert result == ["adhd"]
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_lookup_failure_falls_back_to_new_tags_only():
    """A DB error here must not crash the whole ingest batch — matches the
    existing _find_near_duplicate() convention of degrading gracefully."""
    session = MagicMock()
    session.execute = AsyncMock(side_effect=Exception("boom"))
    result = await _merge_tag_list(session, "domain_tags", "https://example.com/a", ["adhd"])
    assert result == ["adhd"]
