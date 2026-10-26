"""Shared retrieval used by /search and /ask: latest succeeded index -> chunks from PostgreSQL."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import List, Tuple

from sqlalchemy import func, or_
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


_STOPWORDS = {"the", "and", "how", "what", "does", "this", "that", "with", "for", "are", "from", "into", "where", "which", "when", "use", "used", "can", "you"}
RRF_K = 60


def dedupe_overlaps(hits, max_overlap: float = 0.5):
    """Drop lower-ranked chunks that mostly overlap (same file) a better-ranked kept chunk.

    The chunker uses overlapping windows for long code, so near-duplicates are common.
    """
    kept = []
    for chunk, score in hits:
        dup = False
        for k, _ in kept:
            if k.file_path != chunk.file_path:
                continue
            overlap = min(k.end_line, chunk.end_line) - max(k.start_line, chunk.start_line) + 1
            shorter = min(k.end_line - k.start_line, chunk.end_line - chunk.start_line) + 1
            if overlap > 0 and overlap / max(shorter, 1) > max_overlap:
                dup = True
                break
        if not dup:
            kept.append((chunk, score))
    return kept


def query_tokens(query: str):
    toks = []
    for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query.lower()):
        if t not in _STOPWORDS and t not in toks:
            toks.append(t)
    return toks[:8]


def lexical_hits(db: Session, repo_id: int, query: str, limit: int):
    """Portable keyword search (works on PostgreSQL and SQLite): identifier/word matches in symbol name or code."""
    toks = query_tokens(query)
    if not toks:
        return []
    conds = []
    for t in toks:
        pat = "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conds += [CodeChunk.symbol_name.ilike(pat, escape="\\"), CodeChunk.content.ilike(pat, escape="\\")]
    rows = db.query(CodeChunk).filter(CodeChunk.repository_id == repo_id, or_(*conds)).limit(500).all()
    scored = []
    for r in rows:
        name, body = (r.symbol_name or "").lower(), (r.content or "").lower()
        s = sum(3.0 * (t in name) + min(body.count(t), 5) for t in toks)
        if s > 0:
            scored.append((r, s))
    scored.sort(key=lambda x: -x[1])
    return scored[:limit]


def fuse_rrf(vector_hits, lexical):
    """Reciprocal-rank fusion. Returned score stays the VECTOR similarity (0.0 for keyword-only hits)."""
    fused = {}
    for rank, (c, sc) in enumerate(vector_hits):
        fused[c.id] = [c, sc, 1.0 / (RRF_K + rank + 1)]
    for rank, (c, _) in enumerate(lexical):
        if c.id in fused:
            fused[c.id][2] += 1.0 / (RRF_K + rank + 1)
        else:
            fused[c.id] = [c, 0.0, 1.0 / (RRF_K + rank + 1)]
    return [(c, sc) for c, sc, _ in sorted(fused.values(), key=lambda v: -v[2])]


def retrieval_mode(override=None) -> str:
    mode = (override or os.getenv("RETRIEVAL_MODE", "vector")).lower()
    return mode if mode in ("vector", "hybrid") else "vector"


def retrieve(db: Session, repo_id: int, query: str, top_k: int, mode=None) -> Retrieval:
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
        raw = vector_store.search(repo_id, job.id, query, min(max(top_k * 3, top_k), 60))
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
    if retrieval_mode(mode) == "hybrid":
        hits = fuse_rrf(hits, lexical_hits(db, repo_id, query, min(max(top_k * 3, top_k), 60)))
    return Retrieval(job.id, dedupe_overlaps(hits)[:top_k])
