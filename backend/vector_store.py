"""ChromaDB vector store for RepoPilot.

Design (see docs/ARCHITECTURE.md):

* Every ingestion job builds its OWN collection, named ``repo_<repo>_job_<job>``.
* The collection is built BEFORE the database transaction commits. If embedding
  or indexing fails, the exception propagates, the DB transaction is rolled
  back, and the previous data and index stay untouched.
* The committed ``succeeded`` job is the pointer to the live index: search
  always reads the collection of the repository's latest succeeded job. Stale
  collections from older jobs are therefore never queried, and are deleted
  (best effort) only after a newer job has committed.
* Vector IDs are the persisted ``code_chunks.id`` values (as strings), which are
  unique within a job-scoped collection. The chunk text itself lives in the
  database; Chroma stores only vectors and small metadata.

The client is created lazily (not at import time) so importing the app has no
side effects on disk and the storage path can be configured per run.
"""
from __future__ import annotations

import logging
from typing import Iterable, List, Optional, Sequence, Tuple

import chromadb
from chromadb.config import Settings
from chromadb.errors import NotFoundError

from backend.config import get_chroma_path
from backend.embeddings import EmbeddingError, embed, embed_texts

logger = logging.getLogger(__name__)

__all__ = [
    "EmbeddingError",
    "VectorStoreError",
    "collection_name",
    "index_chunks",
    "search",
    "drop_collection",
    "delete_repo_collections",
]

_client = None
_client_path: Optional[str] = None


class VectorStoreError(RuntimeError):
    """Raised when the vector index cannot be built or queried."""


def _get_client():
    """Return the persistent client, (re)creating it if the configured path changed."""
    global _client, _client_path  # noqa: PLW0603
    path = get_chroma_path()
    if _client is None or _client_path != path:
        # ChromaDB's Settings class reads "./.env" from the CURRENT directory by
        # default (and would parse our secrets file). _env_file=None disables that.
        _client = chromadb.PersistentClient(
            path=path, settings=Settings(_env_file=None, anonymized_telemetry=False)
        )
        _client_path = path
    return _client


def collection_name(repo_id: int, job_id: int) -> str:
    return f"repo_{int(repo_id)}_job_{int(job_id)}"


def index_chunks(repo_id: int, job_id: int, chunks: Sequence, batch_size: int = 128) -> int:
    """Embed ``chunks`` and store them in a fresh collection for this job.

    ``chunks`` must be persisted ORM rows (``chunk.id`` set, e.g. after
    ``session.flush()``). Returns the number of vectors stored (0 if there is
    nothing to index; no collection is created in that case).

    Raises :class:`EmbeddingError` or :class:`VectorStoreError` on any failure;
    a partially built collection is removed before the error propagates.
    """
    chunks = list(chunks)
    if not chunks:
        return 0

    ids: List[str] = []
    for c in chunks:
        if c.id is None:
            raise VectorStoreError("Cannot index a chunk without a database id (flush first).")
        if c.repository_id != repo_id:
            raise VectorStoreError(
                f"Chunk {c.id} belongs to repository {c.repository_id}, not {repo_id}."
            )
        ids.append(str(c.id))
    if len(set(ids)) != len(ids):
        raise VectorStoreError("Duplicate chunk ids passed to the vector store.")

    name = collection_name(repo_id, job_id)
    try:
        client = _get_client()
        try:  # a crashed earlier run of the same job id could have left this behind
            client.delete_collection(name)
        except NotFoundError:
            pass
        coll = client.create_collection(name=name, metadata={"hnsw:space": "cosine"})

        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            vectors = embed_texts([c.content or "" for c in batch])
            coll.add(
                ids=ids[start : start + batch_size],
                embeddings=vectors,
                metadatas=[
                    {
                        "repository_id": int(repo_id),
                        "chunk_id": int(c.id),
                        "job_id": int(job_id),
                        "file_path": c.file_path,
                        "symbol_name": c.symbol_name or "",
                        "chunk_type": c.chunk_type,
                    }
                    for c in batch
                ],
            )

        stored = coll.count()
        if stored != len(chunks):
            raise VectorStoreError(
                f"Vector count mismatch: stored {stored}, expected {len(chunks)}."
            )
        return stored
    except Exception as exc:
        drop_collection(repo_id, job_id)
        if isinstance(exc, (EmbeddingError, VectorStoreError)):
            raise
        raise VectorStoreError(f"Vector indexing failed: {exc}") from exc


def search(repo_id: int, job_id: int, query: str, k: int = 5) -> List[Tuple[int, float]]:
    """Return ``[(chunk_id, similarity), ...]`` best-first for one repository/job.

    Similarity is ``1 - cosine_distance`` (1.0 = identical direction).
    Raises if the index is missing or cannot be queried; never returns an empty
    list to hide a failure. (An existing, empty index legitimately returns ``[]``.)
    """
    name = collection_name(repo_id, job_id)
    query_vector = embed(query)  # EmbeddingError propagates
    try:
        coll = _get_client().get_collection(name=name)
    except NotFoundError as exc:
        raise VectorStoreError(
            f"Vector index '{name}' does not exist; re-run ingestion for this repository."
        ) from exc
    except Exception as exc:
        raise VectorStoreError(f"Could not open vector index '{name}': {exc}") from exc

    try:
        count = coll.count()
        if count == 0:
            return []
        res = coll.query(
            query_embeddings=[query_vector],
            n_results=min(int(k), count),
            where={"repository_id": int(repo_id)},  # defence in depth: repo scoping
            include=["distances"],
        )
    except Exception as exc:
        raise VectorStoreError(f"Vector search failed: {exc}") from exc

    return [(int(i), 1.0 - float(d)) for i, d in zip(res["ids"][0], res["distances"][0])]


def drop_collection(repo_id: int, job_id: int) -> bool:
    """Best-effort removal of one job's collection. Returns True if it is gone."""
    name = collection_name(repo_id, job_id)
    try:
        _get_client().delete_collection(name)
        return True
    except NotFoundError:
        return True
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask the original error
        logger.warning("Could not delete vector collection %s: %s", name, type(exc).__name__)
        return False


def delete_repo_collections(repo_id: int, keep_job_id: Optional[int] = None) -> List[str]:
    """Delete every collection of ``repo_id`` except ``keep_job_id``'s.

    Also removes the legacy ``repo_<id>`` collection created by older versions.
    Returns the names that could not be deleted (they are harmless: search only
    reads the collection of the latest succeeded job).
    """
    prefix = f"repo_{int(repo_id)}_job_"
    legacy = f"repo_{int(repo_id)}"
    keep = collection_name(repo_id, keep_job_id) if keep_job_id is not None else None
    failed: List[str] = []
    try:
        names: Iterable[str] = [getattr(c, "name", c) for c in _get_client().list_collections()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not list vector collections: %s", type(exc).__name__)
        return ["<list_collections failed>"]
    for name in names:
        if (name.startswith(prefix) or name == legacy) and name != keep:
            try:
                _get_client().delete_collection(name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not delete stale collection %s: %s", name, type(exc).__name__)
                failed.append(name)
    return failed
