"""Shared retrieval used by /search and /ask: latest succeeded index -> chunks from PostgreSQL."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend import vector_store
from backend.embeddings import EmbeddingError
from backend.models import CodeChunk, IngestionJob
from backend.utils.errors import sanitize_error

logger = logging.getLogger(__name__)


class NotIndexedError(Exception):
    """The repository has no succeeded ingestion job yet (maps to HTTP 409)."""


class IndexUnavailableError(Exception):
    """Embedding model or vector index cannot answer, or is out of sync (maps to HTTP 503)."""


@dataclass
class Retrieval:
    job_id: int
    hits: List[Tuple[CodeChunk, float]]  # best first


def retrieve(db: Session, repo_id: int, query: str, top_k: int) -> Retrieval:
    job = (
        db.query(IngestionJob)
        .filter(IngestionJob.repository_id == repo_id, IngestionJob.status == "succeeded")
        .order_by(IngestionJob.id.desc())
        .first()
    )
    if not job:
        latest = (
            db.query(IngestionJob)
            .filter(IngestionJob.repository_id == repo_id)
            .order_by(IngestionJob.id.desc())
            .first()
        )
        state = f"latest ingestion job #{latest.id} is {latest.status}" if latest else "no ingestion job exists"
        raise NotIndexedError(f"Repository has not been indexed yet ({state}).")

    if not db.query(func.count(CodeChunk.id)).filter(CodeChunk.repository_id == repo_id).scalar():
        return Retrieval(job.id, [])  # indexed successfully, but no Python chunks exist

    try:
        raw = vector_store.search(repo_id, job.id, query, top_k)
    except EmbeddingError as e:
        raise IndexUnavailableError(f"Embedding model unavailable: {sanitize_error(e)}")
    except vector_store.VectorStoreError as e:
        raise IndexUnavailableError(f"Vector index unavailable: {sanitize_error(e)}")

    ids = [cid for cid, _ in raw]
    rows = (
        {c.id: c for c in db.query(CodeChunk).filter(CodeChunk.repository_id == repo_id, CodeChunk.id.in_(ids))}
        if ids else {}
    )
    hits = []
    for cid, score in raw:
        row = rows.get(cid)
        if row is None:
            logger.warning("Vector hit %s has no matching chunk row in repo %s", cid, repo_id)
            continue
        hits.append((row, score))
    if raw and not hits:
        raise IndexUnavailableError(
            "Vector index is out of sync with the database; re-run ingestion for this repository."
        )
    return Retrieval(job.id, hits)
