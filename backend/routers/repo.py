import os
import shutil
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from sqlalchemy.orm import Session

from backend.database import get_db, SessionLocal
from backend.models import Repository, IngestionJob, CodeDefinition, CodeChunk
from backend.schemas import (
    RepositoryCreate,
    IngestResponse,
    CodeDefinitionResponse,
    CodeChunkResponse,
    RepositoryResponse,
    IngestionJobResponse,
    SearchRequest,
    SearchResult,
)
from backend.utils.github import parse_github_url, download_and_extract_repo
from backend.parser.python_ast import parse_repository
from backend.chunker.python_chunker import chunk_repository
# New imports for semantic search integration
from backend import vector_store

import shutil
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from sqlalchemy.orm import Session

from backend.database import get_db, SessionLocal
from backend.models import Repository, IngestionJob, CodeDefinition, CodeChunk
from backend.schemas import (
    RepositoryCreate,
    IngestResponse,
    CodeDefinitionResponse,
    CodeChunkResponse,
    RepositoryResponse,
    IngestionJobResponse,
    SearchRequest,
    SearchResult,
)
from backend.utils.github import parse_github_url, download_and_extract_repo
from backend.parser.python_ast import parse_repository
from backend.chunker.python_chunker import chunk_repository

router = APIRouter(prefix="/repositories", tags=["repositories"])


def process_repository_ingestion(repo_id: int, job_id: int, github_url: str, db_factory=SessionLocal):
    """Worker task to download repository, extract code, parse AST definitions, and generate code chunks.

    Guarantees:
    - Atomicity: Old definitions/chunks are deleted and new ones inserted within a single transaction.
    - Rollback on failure: If parsing/insertion fails, database rolls back to keep previous index intact.
    - Safe filesystem cleanup on failure: If extraction fails, target folder is wiped.
    """
    db: Session = db_factory()
    base_repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "repo_data", str(repo_id)))
    temp_extract_dir = f"{base_repo_dir}_incoming"

    try:
        job = db.query(IngestionJob).filter(IngestionJob.id == job_id).first()
        if not job:
            return

        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        db.commit()

        # Clean any stale incoming directory
        if os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir, ignore_errors=True)

        extracted_dir = download_and_extract_repo(str(github_url), temp_extract_dir)

        # 1. Parse AST definitions in memory
        parsed_definitions = parse_repository(extracted_dir)
        new_definitions = [
            CodeDefinition(
                repository_id=repo_id,
                file_path=def_item.get("file", ""),
                name=def_item.get("name", ""),
                definition_type=def_item.get("type", ""),
                start_line=def_item.get("start_line", 0),
                end_line=def_item.get("end_line", 0),
            )
            for def_item in parsed_definitions
        ]

        # 2. Generate semantic code chunks in memory
        parsed_chunks = chunk_repository(extracted_dir)
        new_chunks = [
            CodeChunk(
                repository_id=repo_id,
                file_path=c.get("file_path", ""),
                chunk_index=c.get("chunk_index", 0),
                chunk_type=c.get("chunk_type", "module_level"),
                symbol_name=c.get("symbol_name"),
                start_line=c.get("start_line", 0),
                end_line=c.get("end_line", 0),
                content=c.get("content", ""),
            )
            for c in parsed_chunks
        ]

        # 3. Atomic Database Replacement in a single transaction
        try:
            # Delete old rows
            db.query(CodeDefinition).filter(CodeDefinition.repository_id == repo_id).delete()
            db.query(CodeChunk).filter(CodeChunk.repository_id == repo_id).delete()

            # Insert new rows
            if new_definitions:
                db.bulk_save_objects(new_definitions)
            if new_chunks:
                db.bulk_save_objects(new_chunks)

            # Flush to assign IDs (optional, commit will assign too)
            db.flush()

            # Move extracted directory to live directory if temp_extract_dir was created
            if os.path.exists(temp_extract_dir):
                if os.path.exists(base_repo_dir):
                    shutil.rmtree(base_repo_dir, ignore_errors=True)
                os.makedirs(os.path.dirname(base_repo_dir), exist_ok=True)
                shutil.move(temp_extract_dir, base_repo_dir)

            job.status = "succeeded"
            job.finished_at = datetime.now(timezone.utc)
            job.log = (
                f"Successfully ingested and extracted. "
                f"Parsed {len(new_definitions)} symbols, generated {len(new_chunks)} chunks."
            )
            db.commit()

            # ---------- Vector store upsert (non‑critical) ----------
            try:
                vector_store.add_chunks(new_chunks)
            except Exception as ve:
                # Log error but do not affect ingestion outcome
                print(f"Vector store error (non‑critical): {ve}")
        
        except Exception:
            db.rollback()
            raise

    except Exception as e:
        # Clean incoming temp dir on failure
        if os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir, ignore_errors=True)

        job = db.query(IngestionJob).filter(IngestionJob.id == job_id).first()
        if job:
            job.status = "failed"
            job.finished_at = datetime.now(timezone.utc)
            job.log = f"Ingestion error: {str(e)}"
            db.commit()
    finally:
        db.close()


@router.post("/", response_model=IngestResponse)
def create_repository(payload: RepositoryCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    url_str = str(payload.github_url).rstrip("/")
    try:
        owner, repo_name = parse_github_url(url_str)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    repo = db.query(Repository).filter(Repository.github_url == url_str).first()
    if not repo:
        repo = Repository(
            github_url=url_str,
            name=f"{owner}/{repo_name}",
        )
        db.add(repo)
        db.commit()
        db.refresh(repo)

    # Check for actively running job to guard concurrent duplicate execution
    active_job = (
        db.query(IngestionJob)
        .filter(IngestionJob.repository_id == repo.id, IngestionJob.status.in_(["pending", "running"]))
        .first()
    )
    if active_job:
        return IngestResponse(
            repository_id=repo.id,
            job_id=active_job.id,
            message=f"An ingestion job (#{active_job.id}) is already {active_job.status} for this repository.",
        )

    job = IngestionJob(
        repository_id=repo.id,
        status="pending",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    background_tasks.add_task(process_repository_ingestion, repo.id, job.id, url_str)

    return IngestResponse(
        repository_id=repo.id,
        job_id=job.id,
        message="Repository ingestion scheduled successfully."
    )


@router.get("/{repo_id}", response_model=RepositoryResponse)
def get_repository(repo_id: int, db: Session = Depends(get_db)):
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    return repo


@router.get("/{repo_id}/jobs", response_model=List[IngestionJobResponse])
def get_repository_jobs(repo_id: int, db: Session = Depends(get_db)):
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    return db.query(IngestionJob).filter(IngestionJob.repository_id == repo_id).order_by(IngestionJob.id.desc()).all()


@router.get("/{repo_id}/definitions", response_model=List[CodeDefinitionResponse])
def get_repository_definitions(
    repo_id: int,
    definition_type: Optional[str] = Query(None, description="Filter by 'function' or 'class'"),
    db: Session = Depends(get_db),
):
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    query = db.query(CodeDefinition).filter(CodeDefinition.repository_id == repo_id)
    if definition_type:
        query = query.filter(CodeDefinition.definition_type == definition_type)

    return query.all()

@router.post("/{repo_id}/search", response_model=List[SearchResult])
def search_repository(repo_id: int, req: SearchRequest, db: Session = Depends(get_db)):
    """Semantic search over code chunks of a repository.
    The vector store is queried with the provided query and top_k limit.
    """
    # Verify repository exists
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    # Perform search via vector store
    results = vector_store.search(repo_id, req.query, k=req.top_k)
    return results


@router.get("/{repo_id}/chunks", response_model=List[CodeChunkResponse])
def get_repository_chunks(
    repo_id: int,
    chunk_type: Optional[str] = Query(None, description="Filter by chunk_type"),
    db: Session = Depends(get_db),
):
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    query = db.query(CodeChunk).filter(CodeChunk.repository_id == repo_id).order_by(CodeChunk.chunk_index)
    if chunk_type:
        query = query.filter(CodeChunk.chunk_type == chunk_type)

    return query.all()

@router.post("/{repo_id}/search", response_model=List[SearchResult])
def search_repository(repo_id: int, req: SearchRequest, db: Session = Depends(get_db)):
    """Semantic search over code chunks of a repository.
    The vector store is queried with the provided query and top_k limit.
    """
    # Verify repository exists
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    # Perform search via vector store
    results = vector_store.search(repo_id, req.query, k=req.top_k)
    return results
