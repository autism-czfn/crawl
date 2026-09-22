"""PDF text extractor using pdfplumber.

Return semantics:
  str  — extracted text (may be empty string '' as permanent-failure sentinel)
  None — extraction not attempted (e.g. locked PDF, import error)

Callers MUST store '' in content_body to mark permanent failure (prevents retry).
Callers MUST NOT store None — leave content_body as NULL to allow future retry.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_MAX_CHARS = 50_000


def extract_text_from_pdf(content: bytes) -> str | None:
    """Extract text from PDF bytes.

    Parameters
    ----------
    content:
        Raw PDF bytes.

    Returns
    -------
    str
        Extracted text, capped at 50,000 characters. Empty string '' means
        the PDF was scanned/image-only or produced no usable text (permanent failure).
    None
        Extraction not attempted (pdfplumber not installed, or PDF is locked/
        encrypted and raises an exception before any text could be read).
    """
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed; PDF extraction skipped")
        return None

    try:
        import io
        pages_text: list[str] = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                try:
                    page_text = page.extract_text() or ""
                    pages_text.append(page_text)
                except Exception as exc:
                    logger.debug("PDF page extraction failed: %s", exc)
                    continue

        full_text = "\n".join(pages_text).strip()

        if not full_text:
            logger.info("PDF extraction produced no text (likely scanned/image-only)")
            return ""  # permanent failure sentinel

        # Strip page-number lines (lines that are just a number)
        lines = full_text.splitlines()
        cleaned_lines = [ln for ln in lines if not re.match(r"^\s*\d+\s*$", ln)]
        full_text = "\n".join(cleaned_lines)

        # Strip everything after "References" section header
        ref_match = re.search(r"\nReferences\s*\n", full_text, re.IGNORECASE)
        if ref_match:
            full_text = full_text[: ref_match.start()]

        full_text = re.sub(r"\s+", " ", full_text).strip()
        return full_text[:_MAX_CHARS] if full_text else ""

    except Exception as exc:
        logger.warning("PDF extraction failed (locked or corrupt PDF): %s", exc)
        return None  # not attempted — allow future retry
