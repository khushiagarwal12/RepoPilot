# RepoPilot

**AI‑powered Codebase Intelligence Assistant**

## Overview
RepoPilot lets a developer paste a public GitHub URL, indexes the repository, and answers natural‑language questions about the code.  It stores metadata in PostgreSQL and embeddings in Chroma.

## Prerequisites (Windows)
- Python 3.11+ (`python --version`)
- Docker Desktop (for PostgreSQL) **or** a local PostgreSQL installation
- Git (`git --version`)
- Node 20+ and npm (for the frontend – not required to run the backend)
- VS Code (recommended editor)
- No API key is needed today: embeddings run locally (`all-MiniLM-L6-v2`, downloaded on first use). An LLM key will only be needed for the planned `/ask` feature (see `docs/ASK_DESIGN.md`).

## Quick start (backend only)
```powershell
# 1️⃣ Clone / create the project folder (you already have it)
cd C:\Users\Khushi Agarwal\Desktop\RepoPilot

# 2️⃣ Create a virtual environment
python -m venv .venv
& .\.venv\Scripts\Activate.ps1

# 3️⃣ Install Python deps
pip install -r requirements.txt

# 3b Create your env file (never commit .env)
Copy-Item .env.example .env   # then edit POSTGRES_URL

# 4️⃣ Spin up PostgreSQL (Docker) – keep it running in a separate terminal
docker compose -f docker-compose.yml up -d

# 5️⃣ Apply DB migrations (once the DB is up)
alembic upgrade head

# 6️⃣ Run the FastAPI server
uvicorn backend.main:app --reload
```
Open <http://127.0.0.1:8000/health> in a browser – you should see `{ "status": "ok" }`.

## What works today (verified by tests)
Submit a public GitHub URL -> safe download/extract -> Python AST parsing + chunking -> PostgreSQL -> local embeddings -> ChromaDB -> per-repository semantic **search**. See `docs/ARCHITECTURE.md`.

| Endpoint | Purpose |
|---|---|
| `POST /repositories/` | Start ingestion, returns `repository_id` and `job_id` |
| `GET /repositories/{id}/jobs` | Job status (`pending/running/succeeded/failed`) and log |
| `GET /repositories/{id}/definitions`, `/chunks` | Parsed symbols and persisted chunks |
| `POST /repositories/{id}/search` | Semantic search (`{query, top_k}`), 409 if not indexed, 503 if index unavailable |

**Not implemented yet:** LLM answers with citations (`/ask`, designed in `docs/ASK_DESIGN.md`), frontend, CI, Docker image for the API. Only Python files are indexed.

## Tests
```powershell
python -m pytest -q
```
Tests use SQLite, real ChromaDB in a temp folder and a small stand-in embedder (no model download). PostgreSQL migrations were verified separately (`alembic upgrade head` / `downgrade base`). First real ingest downloads the embedding model, so allow a few minutes: `python scratch/ingest_test.py --timeout 600`.

## Configuration (`.env`)
`POSTGRES_URL` (required unless `REPOPILOT_ALLOW_SQLITE=1`), `CHROMA_DB_PATH` (default `chroma_store`), `REPOPILOT_ENV_FILE`. Local data folders `chroma_store/`, `repo_data/` are git-ignored.

## Limitations
Single API process (interrupted jobs are marked failed at startup); no authentication or rate limiting; no CORS yet; the embedding model truncates long chunks (~256 tokens).

---
*All secrets belong in a `.env` file that you create from `.env.example`.*
