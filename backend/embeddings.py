"""Embedding utilities for RepoPilot.

A thin wrapper around a sentence-transformers model, loaded lazily on first use
so importing the application stays cheap. Failures are never swallowed: every
problem (missing package, model download failure, bad output) raises
:class:`EmbeddingError` so callers can report it truthfully.
"""
from __future__ import annotations

from typing import List, Sequence

import numpy as np

MODEL_NAME = "all-MiniLM-L6-v2"  # ~22 MB, CPU friendly, 384 dimensions

_model = None


class EmbeddingError(RuntimeError):
    """Raised when text cannot be embedded."""


def _load_model() -> None:
    """Load the embedding model on first use (isolated so tests can stub it)."""
    global _model  # noqa: PLW0603
    if _model is not None:
        return
    try:
        from sentence_transformers import SentenceTransformer  # heavy, optional at import time

        _model = SentenceTransformer(MODEL_NAME)
    except Exception as exc:  # noqa: BLE001 - re-raised as a typed error with context
        raise EmbeddingError(
            f"Failed to load embedding model '{MODEL_NAME}' "
            f"(is 'sentence-transformers' installed and the model downloadable?): {exc}"
        ) from exc


def embed_texts(texts: Sequence[str], batch_size: int = 32) -> List[List[float]]:
    """Embed many texts. Returns one ``list[float]`` per input, in order."""
    texts = list(texts)
    if not texts:
        return []
    _load_model()
    try:
        raw = _model.encode(
            texts,
            batch_size=batch_size,
            convert_to_numpy=True,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        arr = np.asarray(raw, dtype=np.float64)
    except Exception as exc:  # noqa: BLE001
        raise EmbeddingError(f"Embedding computation failed: {exc}") from exc

    if arr.ndim != 2 or arr.shape[0] != len(texts):
        raise EmbeddingError(
            f"Embedding model returned shape {arr.shape} for {len(texts)} input texts."
        )
    if not np.isfinite(arr).all():
        raise EmbeddingError("Embedding model returned non-finite values (NaN/inf).")
    return arr.tolist()


def embed(text: str) -> List[float]:
    """Embed a single text."""
    return embed_texts([text])[0]
