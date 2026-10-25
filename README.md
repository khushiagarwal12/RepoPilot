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
- An API key for either **Google Gemini** or **OpenAI** (for embeddings & LLM calls)

## Quick start (backend only)
```powershell
# 1️⃣ Clone / create the project folder (you already have it)
cd C:\Users\Khushi Agarwal\Desktop\RepoPilot

# 2️⃣ Create a virtual environment
python -m venv .venv
& .\.venv\Scripts\Activate.ps1

# 3️⃣ Install Python deps
pip install -r requirements.txt

# 4️⃣ Spin up PostgreSQL (Docker) – keep it running in a separate terminal
docker compose -f docker-compose.yml up -d

# 5️⃣ Apply DB migrations (once the DB is up)
alembic upgrade head

# 6️⃣ Run the FastAPI server
uvicorn backend.main:app --reload
```
Open <http://127.0.0.1:8000/health> in a browser – you should see `{ "status": "ok" }`.

## Next steps
- Phase 1: implement the `/health` endpoint and its test.
- Phase 2: GitHub ingestion service, etc.

---
*All secrets belong in a `.env` file that you create from `.env.example`.*
