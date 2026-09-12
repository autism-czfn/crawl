"""Tests for playwright_crawl's non-content-title detection — extended after
live-validating hopkins_sleep/hopkins_eating, whose Cloudflare variant titles
its block page "Attention Required! | Cloudflare" rather than the
"Just a Moment..." pattern the collector originally only checked for.
"""
from src.collectors.playwright_crawl import _looks_like_non_content_title


def test_cloudflare_just_a_moment_detected():
    assert _looks_like_non_content_title("Just a moment...") is True


def test_cloudflare_attention_required_detected():
    """The exact title observed live on hopkinsmedicine.org block pages."""
    assert _looks_like_non_content_title("Attention Required! | Cloudflare") is True


def test_bare_403_detected():
    assert _looks_like_non_content_title("403 Forbidden") is True


def test_bare_404_detected():
    assert _looks_like_non_content_title("404 Not Found") is True


def test_real_content_title_not_flagged():
    assert _looks_like_non_content_title("Sleep Conditions") is False
    assert _looks_like_non_content_title("Eating Disorders in Children and Adolescents") is False


def test_none_title_not_flagged():
    assert _looks_like_non_content_title(None) is False


def test_case_insensitive():
    assert _looks_like_non_content_title("JUST A MOMENT...") is True
