"""Embedding utilities for RepoPilot.

This module provides a thin wrapper around a sentence‑transformer model.
It lazily loads the model on first use so import time stays cheap.
The default model is a small, CPU‑only transformer that works without
network access after the first download (cached in the user's home
directory).  If the model cannot be loaded (e.g. missing packages) the
functions raise a clear RuntimeError – callers can decide whether to
fallback or abort.
"""

from __future__ import annotations

from typing import List

# SentenceTransformer is an optional heavy dependency. Import lazily
# to keep the core application start‑up lightweight.
_model = None  # type: ignore[assignment]


def _load_model() -> None:
    """Load the default MiniLM model on first use.

    The function mutates the module‑level ``_model`` variable. It is
    deliberately isolated so unit‑tests can monkey‑patch it with a stub
    implementation that returns deterministic vectors.
    """
    global _model  # noqa: PLW0603
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import]
            # ``all-MiniLM-L6-v2`` is ~22 MB and fast on CPU.
            _model = SentenceTransformer("all-MiniLM-L6-v2")
        except Exception as exc:
            raise RuntimeError("Failed to load embedding model: " + str(exc)) from exc


def embed(text: str) -> List[float]:
    """Return a dense vector for *text*.

    The function guarantees a ``list[float]`` return type – this matches
    what ``chromadb`` expects for a custom embedding function.
    """
    _load_model()
    # ``encode`` returns a NumPy array; convert to plain Python list.
    return _model.encode(text, convert_to_numpy=False).tolist()  # type: ignore[no-any-return]
