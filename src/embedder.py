"""Embedding model helper — fastembed, local, no API key required.

Model : nomic-ai/nomic-embed-text-v1.5
Dims  : 768
Notes :
  - Model is downloaded once (~270 MB) on first call and cached on disk.
  - fastembed.embed() returns numpy.ndarray objects; .tolist() converts to
    plain Python list[float] required by pgvector DB drivers.
  - fastembed is synchronous. Callers running inside an asyncio event loop
    must wrap with asyncio.to_thread(embed_texts, texts).
  - nomic-embed-text-v1.5 requires task prefixes for proper alignment:
      documents: "search_document: " prefix
      queries:   "search_query: " prefix
"""
from __future__ import annotations

from fastembed import TextEmbedding

MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"

DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "

_model: TextEmbedding | None = None


def get_model() -> TextEmbedding:
    """Return the singleton TextEmbedding model, loading it on first call."""
    global _model
    if _model is None:
        _model = TextEmbedding(model_name=MODEL_NAME)  # downloads on first call
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch-embed document texts with 'search_document:' prefix (nomic-embed-text-v1.5 requirement).

    NOTE: fastembed.embed() returns numpy.ndarray objects, NOT Python lists.
    .tolist() is required here — pgvector drivers reject numpy arrays at insert time.
    """
    model = get_model()
    prefixed = [DOCUMENT_PREFIX + t for t in texts]
    return [v.tolist() for v in model.embed(prefixed)]


def embed_query_text(text: str) -> list[float]:
    """Embed a single query text with 'search_query:' prefix."""
    model = get_model()
    prefixed = QUERY_PREFIX + text
    return next(model.embed([prefixed])).tolist()
