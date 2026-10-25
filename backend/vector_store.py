'''Vector store utilities for RepoPilot.

Uses ChromaDB with a persistent SQLite backend (default path ``./chroma``).
Each repository gets its own collection named ``repo_<id>`` – this keeps
vectors isolated and allows per‑repo deletion if needed.
'''

from __future__ import annotations

import os
from typing import List

import chromadb
# embedding_functions is imported for potential future use (not required now)
from chromadb.utils import embedding_functions

from backend.models import CodeChunk  # ORM model for DB rows
from backend.embeddings import embed

# Initialise a persistent client – the directory is ignored by git.
_CLIENT = chromadb.PersistentClient(path=os.getenv("CHROMA_DB_PATH", "./chroma"))


def _collection(repo_id: int):
    """Return (or create) the Chroma collection for *repo_id*."""
    name = f"repo_{repo_id}"
    return _CLIENT.get_or_create_collection(name=name)


def add_chunks(chunks: List[CodeChunk]) -> None:
    """Upsert a list of ``CodeChunk`` objects into the repo's collection.

    The function extracts the textual ``content`` of each chunk, computes
    an embedding via :func:`backend.embeddings.embed`, and stores the
    resulting vector together with minimal metadata.
    """
    if not chunks:
        return
    coll = _collection(chunks[0].repository_id)
    ids = [str(c.id) for c in chunks]
    texts = [c.content or "" for c in chunks]
    # Compute embeddings – Chroma can also accept pre‑computed vectors.
    vectors = [embed(t) for t in texts]
    metadatas = [
        {
            "file": c.file_path,
            "name": c.symbol_name or "",
            "type": c.chunk_type,
        }
        for c in chunks
    ]
    coll.add(ids=ids, embeddings=vectors, metadatas=metadatas, documents=texts)


def search(repo_id: int, query: str, k: int = 5) -> List[dict]:
    """Search the collection for *repo_id* with *query*.

    Returns a list of dictionaries containing the stored metadata plus a
    ``score`` field (higher = more similar). ``score`` is derived from the
    distance returned by Chroma (in the range ``[0, 2]`` for cosine) and
    converted to ``1 - distance`` for readability.
    """
    coll = _collection(repo_id)
    results = coll.query(
        query_embeddings=[embed(query)],
        n_results=k,
        include=["metadatas", "distances"],
    )
    hits: List[dict] = []
    for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
        hit = meta.copy()
        # Convert distance to similarity score (0‑1 range for cosine).
        hit["score"] = 1 - dist
        hits.append(hit)
    return hits
