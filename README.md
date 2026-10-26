# RepoPilot

Index a public GitHub repository, then ask questions that are answered **only from its code**, with file/line citations. If the code doesn't contain the answer, RepoPilot says so instead of guessing.

**Stack:** FastAPI, SQLAlchemy + Alembic + PostgreSQL, ChromaDB, Python `ast`, sentence-transformers (local embeddings), Google Gemini (answers), React + TypeScript (Vite), Pytest/Vitest.

## What is verified
| Feature | How it was checked |
|---|---|
| Safe ingestion: URL allow-list, streamed download with size/time caps, ZIP extraction that skips symlinks and rejects traversal/bombs/devices | Unit tests; the real psf/requests archive extracted (2 symlinks skipped) |
| Python parsing + chunking with repo-relative paths and line numbers | Unit tests; real ingest: 218 symbols, 380 chunks |
| PostgreSQL models + Alembic (up/down/up, no drift, one active job per repo) | Tests against real PostgreSQL 16 (`TEST_POSTGRES_URL`) |
| ChromaDB index per job, failure-safe re-indexing | Integration tests with real Chroma |
| Semantic search (optional hybrid keyword fusion, overlap de-duplication) | Tests; hybrid vs vector still needs `scratch/eval_retrieval.py` on the real model |
| `/ask` with evidence gate, citation validation, prompt-injection handling | Tests with a fake LLM and mocked Gemini HTTP |
| Structured JSON logs with secret redaction, rate limiting, safe 500s, CORS | Tests |
| React UI (submit, status, ask, citations, error/empty states) | 21 Vitest tests, typecheck and production build |

**Not verified:** a live Gemini call, hybrid retrieval quality, the Docker images and the GitHub Actions workflow (written but never executed in my environment), a real-browser end-to-end run.

## Setup (Windows PowerShell)
```powershell
git clone https://github.com/khushiagarwal12/RepoPilot.git; cd RepoPilot
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env          # edit POSTGRES_URL and GEMINI_API_KEY; never commit .env
docker compose up -d postgres        # database only
alembic upgrade head
uvicorn backend.main:app --reload    # http://127.0.0.1:8000/docs
```
Frontend (second terminal): `cd frontend; npm install; npm run dev` then open http://localhost:5173.

Everything in containers (unverified here): `docker compose --profile app up --build` -> UI on :8080, API on :8000. Data lives in named volumes (`pg_data`, `chroma_data`, `repo_data`, `hf_cache`).

## Demo workflow
1. Paste `https://github.com/psf/requests` and press **Index**. The status badge moves pending -> running -> succeeded (the first run downloads the embedding model; the backend reports no percentage, so none is shown).
2. Ask "How are redirects handled?". The answer cites `file:lines` with snippets.
3. Ask something unrelated ("capital of France?"). You get "not enough evidence" with no LLM call.

## Tests
```powershell
python -m pytest -q                          # backend (SQLite + real Chroma + stub embedder)
$env:TEST_POSTGRES_URL="postgresql://postgres:<pw>@localhost:5432/repopilot_test"; python -m pytest tests/test_migrations_postgres.py
cd frontend; npm test; npm run build
python scratch/ingest_test.py --timeout 600  # real end-to-end ingest against a running API
```

## Configuration
`POSTGRES_URL` (required unless `REPOPILOT_ALLOW_SQLITE=1`), `GEMINI_API_KEY`, `LLM_MODEL`, `ASK_MIN_SCORE` (0.20, **uncalibrated**), `RETRIEVAL_MODE` (`vector`|`hybrid`), `RATE_LIMIT_PER_MINUTE` (30, 0=off), `CORS_ORIGINS`, `CHROMA_DB_PATH`, `LOG_LEVEL`, `LLM_TIMEOUT_SECONDS`. See `.env.example`.

## Limitations
Python files only; single API process (interrupted jobs are marked failed at startup); no authentication; in-memory rate limiter; embedding model truncates long chunks; answers are stateless (no chat history); `ASK_MIN_SCORE` needs calibration with real queries.

More: `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/ASK_DESIGN.md`, `docs/INTERVIEW_NOTES.md`.
