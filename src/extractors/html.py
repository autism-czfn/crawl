"""Shared HTML body extractor used by html_crawl, playwright_crawl, and sitemap collectors.

Extraction strategy:
  1. Try Trafilatura (best for article extraction, handles boilerplate)
  2. Fall back to CSS selector list
  3. Fall back to raw body text
Quality gate: minimum 300 characters AND 50 words.
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Cookie/paywall boilerplate phrases to reject
_BOILERPLATE_PHRASES = [
    "we use cookies",
    "accept all cookies",
    "cookie preferences",
    "privacy preferences",
    "sign in to continue",
    "sign in to read",
    "log in to continue",
    "create an account",
    "subscribe to read",
    "subscribe to continue",
    "access denied",
    "please enable javascript",
]

# CSS selectors tried in priority order when Trafilatura fails
_SELECTORS = [
    "article",
    "[role='main']",
    "main",
    ".entry-content",
    ".post-content",
    ".article-body",
    ".content-body",
    "#content",
    "#main-content",
    ".content",
]


def extract_body(soup: BeautifulSoup, custom_selectors: list[str] | None = None) -> str | None:
    """Extract main article body text from a BeautifulSoup object.

    Parameters
    ----------
    soup:
        Parsed HTML page.
    custom_selectors:
        Optional list of CSS selectors to try before the default list.
        Pass per-surface selectors here.

    Returns
    -------
    str | None
        Cleaned article body text, or None if extraction failed quality gate.
    """
    # Try Trafilatura first — purpose-built for article extraction
    try:
        import trafilatura
        html_str = str(soup)
        result = trafilatura.extract(
            html_str,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )
        if result and _passes_quality_gate(result):
            return _clean_text(result)
    except ImportError:
        logger.debug("trafilatura not installed; falling back to CSS selectors")
    except Exception as exc:
        logger.debug("trafilatura extraction failed: %s", exc)

    # Fall back to CSS selectors
    work = BeautifulSoup(str(soup), "html.parser")
    for tag in work.find_all(["nav", "footer", "aside", "header", "script", "style", "noscript"]):
        tag.decompose()

    selectors_to_try = (custom_selectors or []) + _SELECTORS
    for sel in selectors_to_try:
        el = work.select_one(sel)
        if el:
            text = el.get_text(" ", strip=True)
            if _passes_quality_gate(text):
                return _clean_text(text)

    # Last resort: raw body
    body = work.find("body")
    if body:
        text = body.get_text(" ", strip=True)
        if _passes_quality_gate(text):
            return _clean_text(text)

    return None


def _passes_quality_gate(text: str) -> bool:
    """Return True only if text meets minimum quality thresholds."""
    if not text or len(text) < 300:
        return False
    words = text.split()
    if len(words) < 50:
        return False
    text_lower = text.lower()
    for phrase in _BOILERPLATE_PHRASES:
        if phrase in text_lower and len(text) < 1000:
            # Short text dominated by boilerplate — reject
            return False
    return True


def _clean_text(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None
