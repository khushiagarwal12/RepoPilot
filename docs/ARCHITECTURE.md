# RepoPilot Architecture (as implemented)

```
POST /repositories/ -> Repository + IngestionJob rows -> BackgroundTask
  download zipball (allow-listed hosts, size/time caps) -> safe_extract_zip (staging dir, symlinks skipped)
  -> ast parse + chunk (repo-relative paths) -> ONE DB transaction:
       replace definitions/chunks, flush (real chunk ids)
       build Chroma collection repo_<repo>_job_<job> (embeddings)   <- before commit
       commit job=succeeded
  -> delete older collections (best effort)
POST /repositories/{id}/search -> latest SUCCEEDED job -> its collection -> hydrate chunks from PostgreSQL
```

## Components
- `backend/routers/repo.py` API + ingestion worker; `backend/utils/github.py` URL validation, download, safe extraction.
- `backend/parser`, `backend/chunker`: Python `ast` only. Repository code is never imported or executed.
- `backend/models.py` + `alembic/`: repositories, ingestion_jobs, code_definitions, code_chunks.
- `backend/embeddings.py` (lazy sentence-transformers), `backend/vector_store.py` (lazy persistent Chroma).
- `backend/config.py`: project-root anchored paths and explicit `.env` loading.

## PostgreSQL vs ChromaDB consistency (no shared transaction)
- Each job has its own collection; the committed `succeeded` job is the pointer to the live index.
- Embedding/index failure -> DB rollback, half-built collection dropped, previous data and index untouched, job `failed` with a sanitized error.
- Crash between index build and commit leaves an orphan collection: never queried, removed after the next successful ingest of that repository.
- Cleanup of old collections after commit may fail without affecting correctness (search ignores them).
- A database partial unique index allows only one pending/running job per repository; jobs left active by a restart are marked failed at startup.

## Known limits
Python files only; single process; no auth/rate limiting; no LLM answering yet (`docs/ASK_DESIGN.md`).
