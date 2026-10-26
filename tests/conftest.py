"""Shared test fixtures.

Every test runs against a private temporary ChromaDB directory (never the
project's real ``chroma_store``/``chroma`` folders) and, unless a test opts out,
a small deterministic embedding model so no model download is needed.
The vector store itself is REAL ChromaDB; only the neural network is replaced.
"""
import hashlib
import os
import re

import numpy as np
import pytest

# Tests use isolated SQLite databases: opt in explicitly BEFORE any backend module is imported.
os.environ.setdefault("REPOPILOT_ALLOW_SQLITE", "1")

import backend.embeddings as embeddings  # noqa: E402

_DIM = 64


class StubEmbeddingModel:
    """Deterministic bag-of-words hashing embedder (similar words => similar vectors)."""

    def encode(self, texts, batch_size=32, convert_to_numpy=True, show_progress_bar=False,
               normalize_embeddings=True):
        out = np.zeros((len(texts), _DIM), dtype=np.float64)
        for i, text in enumerate(texts):
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text.lower()):
                h = int(hashlib.md5(token.encode()).hexdigest(), 16)
                out[i, h % _DIM] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return out / norms


@pytest.fixture(autouse=True)
def isolated_chroma_and_embeddings(tmp_path, monkeypatch):
    monkeypatch.setenv("CHROMA_DB_PATH", str(tmp_path / "chroma_test_store"))
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "0")
    monkeypatch.setattr(embeddings, "_model", StubEmbeddingModel())
    yield
