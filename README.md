# RepoPilot

**Live Demo:** [http://35.234.220.163/](http://35.234.220.163/)  
**GitHub Repository:** [khushiagarwal12/RepoPilot](https://github.com/khushiagarwal12/RepoPilot)

RepoPilot is a code-grounded repository Q&A application that lets users index a public GitHub repository and ask natural-language questions about its Python code. It retrieves relevant code, generates answers using Google Gemini, and provides file-and-line citations so users can verify the evidence. An evidence gate prevents unsupported questions from triggering an LLM response when the retrieved code does not meet the configured relevance threshold.

## Key Features

- **Repository Ingestion:** Validates public GitHub URLs and safely processes repository archives with download limits and ZIP extraction safeguards.
- **Code Parsing and Chunking:** Uses Python's AST to extract code symbols and create searchable chunks with repository-relative paths and line numbers.
- **Semantic Retrieval:** Uses local sentence-transformer embeddings and ChromaDB for vector search, with optional hybrid keyword retrieval and overlap de-duplication.
- **Grounded LLM Answers:** Integrates Google Gemini to answer questions using retrieved code evidence.
- **Citation Validation:** Validates citations against retrieved evidence and returns code snippets for verification.
- **Evidence-Based Rejection:** Rejects questions that lack sufficient supporting evidence instead of encouraging unsupported answers.
- **Reliable Re-indexing:** Maintains job-specific vector indexes and handles failed re-indexing without prematurely replacing the active index.
- **Backend Reliability and Security:** Includes request validation, structured JSON logging, secret redaction, rate limiting, safe error responses, and CORS configuration.

## Tech Stack

| Component | Technologies |
|---|---|
| Backend | Python, FastAPI |
| Database and Migrations | PostgreSQL, SQLAlchemy, Alembic |
| Vector Database | ChromaDB |
| Code Analysis | Python AST |
| Embeddings | Sentence-Transformers |
| LLM Integration | Google Gemini, optional OpenRouter fallback |
| Frontend | React, TypeScript, Vite |
| Testing | Pytest, Vitest |
| Deployment | Docker Compose, Google Cloud VM |

## Architecture Overview

1. **Ingest:** Validate a repository URL, download its archive within configured limits, and extract permitted files safely.
2. **Parse:** Extract Python code structure and split source into searchable chunks while preserving file paths and line numbers.
3. **Index:** Generate embeddings locally and store vectors in ChromaDB, associated with the repository's indexing job.
4. **Retrieve:** Search for code relevant to the user's question using the configured retrieval strategy.
5. **Generate:** Apply an evidence gate before calling the configured LLM and validate generated citations against the retrieved evidence.
6. **Respond:** Return the answer with supporting code references, or indicate that the available evidence is insufficient.

## Verification and Testing

| Area | Verification performed |
|---|---|
| Safe ingestion | Unit tests; extracted the real `psf/requests` archive, skipping two symlinks |
| Parsing and chunking | Unit tests; real ingestion produced 218 symbols and 380 chunks |
| PostgreSQL and migrations | Tests against PostgreSQL 16, including migration up/down/up, schema drift, and active-job constraints |
| ChromaDB indexing | Integration tests with real ChromaDB, including failure-safe re-indexing |
| Retrieval | Tests for semantic retrieval, optional hybrid keyword fusion, and overlap de-duplication |
| LLM answer handling | Tests using a fake LLM and mocked Gemini HTTP responses for evidence gating, citation validation, and prompt-injection handling |
| Backend safeguards | Tests for structured logs, secret redaction, rate limiting, CORS, and safe error responses |
| Frontend | 21 Vitest tests, TypeScript typecheck, and production build |
| Live deployment | Deployed on a Google Cloud VM; Gemini generation verified successfully against the configured model |

**Still requiring additional verification:** Comparative hybrid-versus-vector retrieval quality on a real evaluation set, automated end-to-end browser testing, and execution of the Docker and GitHub Actions workflows in a clean environment.

## Live Demo

1. Open the [RepoPilot web application](http://35.234.220.163/).
2. Submit a public Python repository, such as [`psf/requests`](https://github.com/psf/requests), for indexing.
3. Wait for indexing to complete.
4. Ask a question such as *"How are redirects handled?"* and inspect the answer's file-and-line citations.
5. Try an unrelated question, such as *"What is the capital of France?"*, to observe the evidence gate's insufficient-evidence behavior.

The first indexing run may take longer because the embedding model needs to be downloaded or initialized.

## Local Setup — Windows PowerShell

### Prerequisites

- Python and pip
- Node.js and npm
- Docker Desktop with Docker Compose
- A Google Gemini API key

### 1. Clone and configure

```powershell
git clone https://github.com/khushiagarwal12/RepoPilot.git
cd RepoPilot

python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
Copy-Item .env.example .env
```

Configure the required database connection and Gemini API key in `.env`. **Never commit `.env` or API keys to Git.**

### 2. Start the database and backend

```powershell
docker compose up -d postgres
alembic upgrade head
uvicorn backend.main:app --reload
```

The API and interactive API documentation are available at:

- API: `http://127.0.0.1:8000`
- Swagger UI: `http://127.0.0.1:8000/docs`

### 3. Start the frontend

In a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`.

### Docker Deployment

The application can also be started using the Compose app profile:

```bash
docker compose --profile app up --build -d
```

The Compose configuration exposes the web application and API on their configured ports. Check `docker-compose.yml` for the exact port mappings.

## Running Tests

### Backend

```powershell
python -m pytest -q
```

### PostgreSQL Migration Tests

Set `TEST_POSTGRES_URL` to a dedicated test database before running:

```powershell
$env:TEST_POSTGRES_URL="postgresql://postgres:<pw>@localhost:5432/repopilot_test"
python -m pytest tests/test_migrations_postgres.py
```

### Frontend

```powershell
cd frontend
npm test
npm run build
```

### Real Ingestion Test

With a configured, running API:

```powershell
python scratch/ingest_test.py --timeout 600
```

## Configuration

See `.env.example` for the full configuration reference. Important settings include:

| Variable | Purpose |
|---|---|
| `POSTGRES_URL` | PostgreSQL connection string |
| `GEMINI_API_KEY` | Gemini API authentication |
| `LLM_PROVIDER` | Primary LLM provider |
| `LLM_MODEL` | Primary model identifier |
| `LLM_FALLBACK_PROVIDER` | Optional fallback provider |
| `LLM_FALLBACK_MODEL` | Optional fallback model identifier |
| `OPENROUTER_API_KEY` | Optional OpenRouter authentication |
| `LLM_MAX_RETRIES` | Retry limit for eligible transient failures |
| `LLM_TIMEOUT_SECONDS` | LLM request timeout |
| `ASK_MIN_SCORE` | Evidence threshold; default `0.20`, not yet calibrated |
| `RETRIEVAL_MODE` | `vector` or `hybrid` |
| `RATE_LIMIT_PER_MINUTE` | API rate limit; `0` disables it |
| `CORS_ORIGINS` | Allowed frontend origins |
| `CHROMA_DB_PATH` | ChromaDB storage path |
| `LOG_LEVEL` | Application logging level |

## LLM Retries and Fallback

RepoPilot uses Google Gemini by default. OpenRouter can optionally serve as a fallback.

Add the following to your **local `.env`**, using your own key:

```dotenv
OPENROUTER_API_KEY=<your-key>
LLM_FALLBACK_PROVIDER=openrouter
LLM_FALLBACK_MODEL=google/gemini-2.5-flash
```

Eligible transient failures, including HTTP 429/503 responses and timeouts, are retried with exponential backoff according to `LLM_MAX_RETRIES`. Authentication errors and other non-transient client errors are not retried. If the primary provider continues to fail or is unavailable, the configured fallback is attempted. If both providers fail, the API returns a sanitized error response.

Evidence gating and citation validation are applied regardless of which provider answers. The response's `model` field identifies the model used.

## Limitations

- Python files only.
- No user authentication.
- Single API process; interrupted jobs are marked failed at startup.
- In-memory rate limiting.
- Long chunks may be truncated by the embedding model.
- Answers are stateless and do not maintain conversational history.
- The evidence threshold requires calibration against representative real-world queries.

## Further Documentation

- `docs/ARCHITECTURE.md`
- `docs/DECISIONS.md`
- `docs/ASK_DESIGN.md`
- `docs/INTERVIEW_NOTES.md`
