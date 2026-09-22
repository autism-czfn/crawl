"""Content chunker: splits content_body into 500-1000 token chunks for embedding.

Chunks are stored in the `chunks` table with per-chunk embeddings.
Token estimation uses a simple word-based heuristic (1 token ≈ 0.75 words).

Overlap: each chunk (except the last) carries a ~15% prefix from the
following chunk so that retrieval never misses a sentence that straddles a
boundary.  The overlap is implemented in the merge loop: when a new chunk
starts, the last _OVERLAP_RATIO fraction of the *previous* chunk's words are
prepended as a prefix.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Target chunk size in estimated tokens
_MIN_TOKENS = 400
_MAX_TOKENS = 1000
_TARGET_TOKENS = 700

# Rough token estimation: 1 token ≈ 0.75 words (conservative for English)
_WORDS_PER_TOKEN = 0.75

# Sliding overlap: carry forward this fraction of the previous chunk's tokens
_OVERLAP_RATIO = 0.15


def estimate_tokens(text: str) -> int:
    """Estimate token count from text using word-based heuristic."""
    words = len(text.split())
    return int(words / _WORDS_PER_TOKEN)


def _overlap_prefix(text: str, ratio: float = _OVERLAP_RATIO) -> str:
    """Return the last ~ratio fraction of words from *text* as an overlap prefix."""
    words = text.split()
    n = max(1, int(len(words) * ratio))
    return " ".join(words[-n:])


def chunk_text(text: str, min_tokens: int = _MIN_TOKENS, max_tokens: int = _MAX_TOKENS) -> list[str]:
    """Split text into overlapping chunks of approximately min_tokens to max_tokens.

    Strategy:
    1. Split on paragraph boundaries (double newline)
    2. If a paragraph is too long, split on sentence boundaries
    3. Merge small paragraphs into chunks until target size reached
    4. Apply ~15% sliding overlap: when starting a new chunk, prepend the last
       ~15% of tokens from the previous chunk so context is preserved at
       chunk boundaries.

    Returns list of chunk strings. Each chunk is stripped and non-empty.
    """
    if not text or not text.strip():
        return []

    # Normalize whitespace but preserve paragraph breaks
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Split into paragraphs
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]

    if not paragraphs:
        return []

    # Split long paragraphs into sentences
    segments: list[str] = []
    for para in paragraphs:
        if estimate_tokens(para) > max_tokens:
            # Split on sentence boundaries
            sentences = re.split(r'(?<=[.!?])\s+', para)
            segments.extend(s for s in sentences if s.strip())
        else:
            segments.append(para)

    # Merge segments into chunks with sliding overlap
    chunks: list[str] = []
    current_parts: list[str] = []
    current_tokens = 0
    overlap_prefix: str = ""  # last ~15% of the previous chunk

    for segment in segments:
        seg_tokens = estimate_tokens(segment)

        # If single segment exceeds max, force it as its own chunk
        if seg_tokens > max_tokens:
            # Flush current
            if current_parts:
                chunk_text_val = ' '.join(current_parts)
                chunks.append(chunk_text_val)
                overlap_prefix = _overlap_prefix(chunk_text_val)
                current_parts = []
                current_tokens = 0
            chunks.append(segment)
            overlap_prefix = _overlap_prefix(segment)
            continue

        # If adding this segment would exceed max, flush current
        if current_tokens + seg_tokens > max_tokens and current_parts:
            chunk_text_val = ' '.join(current_parts)
            chunks.append(chunk_text_val)
            overlap_prefix = _overlap_prefix(chunk_text_val)
            # Start new chunk with overlap prefix from previous chunk
            if overlap_prefix:
                current_parts = [overlap_prefix]
                current_tokens = estimate_tokens(overlap_prefix)
            else:
                current_parts = []
                current_tokens = 0

        current_parts.append(segment)
        current_tokens += seg_tokens

    # Flush remaining
    if current_parts:
        chunks.append(' '.join(current_parts))

    # Filter out empty/tiny chunks (merge last tiny chunk with previous)
    result: list[str] = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        if result and estimate_tokens(chunk) < min_tokens // 2:
            # Merge tiny trailing chunk with previous
            result[-1] = result[-1] + ' ' + chunk
        else:
            result.append(chunk)

    return result


# ---------------------------------------------------------------------------
# TODO (future): wire up heading-aware chunking once content_body storage is
# restructured to preserve HTML heading structure.  Use chunk_structured() +
# extract_structured() from src/extractors/html.py for section-level chunks.
# ---------------------------------------------------------------------------


def chunk_structured(sections: list[dict], min_tokens: int = _MIN_TOKENS, max_tokens: int = _MAX_TOKENS) -> list[str]:
    """Chunk a list of heading-tagged sections into overlapping chunks.

    Each section is a dict with keys:
      - "heading": str  (may be empty)
      - "text": str

    For each section, the heading is prepended as "[Heading] " if non-empty,
    then the same paragraph/sentence splitting + overlap logic from chunk_text()
    is applied.  Results are a flat list of chunk strings.

    NOTE: Not yet wired into the pipeline — requires content_body to preserve
    HTML structure.  See TODO above.
    """
    result: list[str] = []
    for section in sections:
        heading = (section.get("heading") or "").strip()
        body = (section.get("text") or "").strip()
        if not body:
            continue
        # Prepend heading marker so the chunk carries section context
        prefixed = f"[{heading}] {body}" if heading else body
        result.extend(chunk_text(prefixed, min_tokens=min_tokens, max_tokens=max_tokens))
    return result
