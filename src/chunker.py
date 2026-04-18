"""Content chunker: splits content_body into 500-1000 token chunks for embedding.

Chunks are stored in the `chunks` table with per-chunk embeddings.
Token estimation uses a simple word-based heuristic (1 token ≈ 0.75 words).
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


def estimate_tokens(text: str) -> int:
    """Estimate token count from text using word-based heuristic."""
    words = len(text.split())
    return int(words / _WORDS_PER_TOKEN)


def chunk_text(text: str, min_tokens: int = _MIN_TOKENS, max_tokens: int = _MAX_TOKENS) -> list[str]:
    """Split text into chunks of approximately min_tokens to max_tokens.

    Strategy:
    1. Split on paragraph boundaries (double newline)
    2. If a paragraph is too long, split on sentence boundaries
    3. Merge small paragraphs into chunks until target size reached

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

    # Merge segments into chunks
    chunks: list[str] = []
    current_parts: list[str] = []
    current_tokens = 0

    for segment in segments:
        seg_tokens = estimate_tokens(segment)

        # If single segment exceeds max, force it as its own chunk
        if seg_tokens > max_tokens:
            # Flush current
            if current_parts:
                chunks.append(' '.join(current_parts))
                current_parts = []
                current_tokens = 0
            chunks.append(segment)
            continue

        # If adding this segment would exceed max, flush current
        if current_tokens + seg_tokens > max_tokens and current_parts:
            chunks.append(' '.join(current_parts))
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
