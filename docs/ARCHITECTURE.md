# RepoPilot Architecture & System Design

RepoPilot is an AI-powered codebase intelligence assistant designed to ingest GitHub repositories, extract AST code definitions, generate semantic code chunks, index embeddings in a vector database, and answer questions via retrieval-augmented generation (RAG).

```mermaid
flowchart TD
    Client["Client / API Consumer"] -->|POST /repositories/| Router["FastAPI Ingestion Router"]
    Client -->|GET /repositories/{id}/chunks| Router
    Client -->|GET /health| Health["Health Endpoint"]

    subgraph Ingestion Pipeline [Asynchronous Ingestion Worker]
        Router -->|Background Task| Worker["process_repository_ingestion"]
        Worker -->|1. Download Zipball & Safe Extract| GH["GitHub Archive Service (Zip Slip Protected)"]
        GH -->|Extracted Code| FS["Local Filesystem (repo_data/{repo_id})"]
        FS -->|2. AST Visitor| Parser["Python AST Parser (functions & classes)"]
        FS -->|3. AST Semantic Slicing| Chunker["Python Semantic Chunker"]
        
        Parser -->|CodeDefinition Records| DB[("PostgreSQL / SQLite")]
        Chunker -->|CodeChunk Records| DB
        Worker -->|Job Status: Succeeded/Failed| DB
    end

    subgraph Future Phase 5
        Chunker -.->|Chunks| VectorStore[("ChromaDB Vector Store")]
    end
```

## System Components

1. **API Layer (`backend/main.py`, `backend/routers/`)**:
   - `GET /health`: Liveness probe.
   - `POST /repositories/`: Validates GitHub URL, stores repository & job records, triggers asynchronous ingestion worker.
   - `GET /repositories/{repo_id}`: Fetches repository metadata.
   - `GET /repositories/{repo_id}/jobs`: Fetches job status and logs.
   - `GET /repositories/{repo_id}/definitions`: Lists parsed functions and classes (supports `?definition_type=` filter).
   - `GET /repositories/{repo_id}/chunks`: Lists semantically chunked code snippets (supports `?chunk_type=` filter).

2. **Ingestion & Security Layer (`backend/utils/github.py`)**:
   - Validates GitHub URLs (rejects non-GitHub domains).
   - Downloads GitHub zipball stream with timeouts.
   - **Security**: Validates against Zip Slip (path traversal) and decompression bombs (file count & uncompressed byte caps).

3. **AST Parser Layer (`backend/parser/python_ast.py`)**:
   - Traverses Python AST using `ast.NodeVisitor`.
   - Extracts top-level classes and functions with exact 1-based start and end line ranges.
   - Skips syntax errors and empty files cleanly.

4. **Chunking Layer (`backend/chunker/python_chunker.py`)**:
   - Generates AST-aware semantic chunks.
   - Slices oversized functions using sliding windows with line overlap.
   - Captures module-level constants and imports.

5. **Storage Layer (`backend/models.py`, `backend/database.py`, `alembic/`)**:
   - Relational store managed via SQLAlchemy ORM and Alembic migrations.
   - Tables: `repositories`, `ingestion_jobs`, `code_definitions`, `code_chunks`.
