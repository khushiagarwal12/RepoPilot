import logging
import os
import shutil
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import vector_store
from backend.chunker.python_chunker import chunk_repository
from backend.config import get_repo_data_dir
from backend.database import SessionLocal, get_db
from backend.embeddings import EmbeddingError
from backend.models import CodeChunk, CodeDefinition, IngestionJob, Repository
from backend.parser.python_ast import parse_repository
from backend.schemas import (
    CodeChunkResponse,
    CodeDefinitionResponse,
    IngestionJobResponse,
    IngestResponse,
    RepositoryCreate,
    RepositoryResponse,
    SearchRequest,
    SearchResult,
)
from backend.utils.errors import sanitize_error
from backend.utils.github import download_and_extract_repo, parse_github_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/repositories", tags=["repositories"])


def process_repository_ingestion(repo_id: int, job_id: int, github_url: str, db_factory=SessionLocal):
    """Worker task: download, parse, chunk, persist and vector-index a repository.

    Success criteria: a job is marked ``succeeded`` only when the database rows
    AND the vector index for this job are both in place.

    Ordering / recovery guarantees:
    1. Old definitions/chunks are replaced inside ONE database transaction.
    2. The job's vector collection is built BEFORE that transaction commits.
       Any extraction, parsing, embedding or indexing error rolls the
       transaction back, removes the half-built collection, and marks the job
       ``failed`` with a sanitized error. The previous data and index (from the
       last succeeded job) remain intact and queryable.
    3. Only after the commit are older collections deleted. If that cleanup
       fails the job is still ``succeeded``: search only reads the collection of
       the latest succeeded job, so leftovers are never returned.
    """
    db: Session = db_factory()
    base_repo_dir = str(get_repo_data_dir() / str(repo_id))
    temp_extract_dir = f"{base_repo_dir}_incoming"

    stage = "starting"
    collection_built = False
    try:
        job = db.query(IngestionJob).filter(IngestionJob.id == job_id).first()
        if not job:
            return

        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        db.commit()

        if os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir, ignore_errors=True)

        stage = "download and extraction"
        extracted_dir = download_and_extract_repo(str(github_url), temp_extract_dir)

        stage = "parsing"
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

        stage = "database update"
        db.query(CodeDefinition).filter(CodeDefinition.repository_id == repo_id).delete()
        db.query(CodeChunk).filter(CodeChunk.repository_id == repo_id).delete()
        # add_all + flush (not bulk_save_objects) so every chunk gets its primary key,
        # which is the vector ID.
        db.add_all(new_definitions)
        db.add_all(new_chunks)
        db.flush()

        stage = "vector indexing"
        indexed = vector_store.index_chunks(repo_id, job_id, new_chunks)
        collection_built = indexed > 0

        stage = "finalizing"
        if os.path.exists(temp_extract_dir):
            if os.path.exists(base_repo_dir):
                shutil.rmtree(base_repo_dir, ignore_errors=True)
            os.makedirs(os.path.dirname(base_repo_dir), exist_ok=True)
            shutil.move(temp_extract_dir, base_repo_dir)

        job.status = "succeeded"
        job.finished_at = datetime.now(timezone.utc)
        if new_chunks:
            index_note = f"indexed {indexed} vectors"
        else:
            index_note = "no indexable Python chunks found, nothing to index"
        job.log = (
            f"Successfully ingested. Parsed {len(new_definitions)} symbols, "
            f"generated {len(new_chunks)} chunks, {index_note}."
        )
        db.commit()
        collection_built = False  # committed: this job's collection is now the live index

        # Post-commit cleanup of superseded indexes (best effort, never changes job status).
        leftovers = vector_store.delete_repo_collections(
            repo_id, keep_job_id=job_id if new_chunks else None
        )
        if leftovers:
            logger.warning("Repo %s: %d stale vector collection(s) not deleted", repo_id, len(leftovers))

    except Exception as e:
        db.rollback()
        if collection_built:
            vector_store.drop_collection(repo_id, job_id)
        if os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir, ignore_errors=True)

        job = db.query(IngestionJob).filter(IngestionJob.id == job_id).first()
        if job:
            job.status = "failed"
            job.finished_at = datetime.now(timezone.utc)
            job.log = f"Ingestion failed during {stage}: {sanitize_error(e)}"
            db.commit()
        logger.error("Ingestion job %s failed during %s: %s", job_id, stage, type(e).__name__)
    finally:
        db.close()


def recover_interrupted_jobs(db_factory=SessionLocal) -> int:
    """Mark pending/running jobs as failed. Call once at startup.

    Ingestion runs in this process's background tasks, so after a restart no such
    job can still be running; leaving them active would block re-indexing forever.
    (Assumes a single API process; revisit if you run several workers.)
    """
    db = db_factory()
    try:
        jobs = db.query(IngestionJob).filter(IngestionJob.status.in_(["pending", "running"])).all()
        for job in jobs:
            job.status = "failed"
            job.finished_at = datetime.now(timezone.utc)
            job.log = "Interrupted: the server restarted before this job finished. Submit the repository again."
        db.commit()
        return len(jobs)
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
        try:
            db.commit()
        except IntegrityError:  # another request created the same repository first
            db.rollback()
            repo = db.query(Repository).filter(Repository.github_url == url_str).first()
            if repo is None:
                raise
        else:
            db.refresh(repo)

    # Guard against concurrent duplicate execution
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

    job = IngestionJob(repository_id=repo.id, status="pending")
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        # The database allows only one pending/running job per repository, so a
        # concurrent request won the race: report that job instead of failing.
        db.rollback()
        active_job = (
            db.query(IngestionJob)
            .filter(IngestionJob.repository_id == repo.id, IngestionJob.status.in_(["pending", "running"]))
            .first()
        )
        if active_job is None:
            raise
        return IngestResponse(
            repository_id=repo.id,
            job_id=active_job.id,
            message=f"An ingestion job (#{active_job.id}) is already {active_job.status} for this repository.",
        )
    db.refresh(job)

    background_tasks.add_task(process_repository_ingestion, repo.id, job.id, url_str)

    return IngestResponse(
        repository_id=repo.id,
        job_id=job.id,
        message="Repository ingestion scheduled successfully.",
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
def search_repository(repo_id: int, req: SearchRequest, response: Response, db: Session = Depends(get_db)):
    """Semantic search over the code chunks of ONE repository.

    Reads the vector index built by the repository's latest *succeeded*
    ingestion job and returns persisted chunks (with ids, line ranges and
    content) ordered by similarity.

    Errors are explicit, never an empty list that hides a failure:
    404 unknown repository; 409 not indexed yet; 503 embedding model or
    vector index unavailable / out of sync.
    """
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

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
        raise HTTPException(status_code=409, detail=f"Repository has not been indexed yet ({state}).")

    chunk_total = db.query(func.count(CodeChunk.id)).filter(CodeChunk.repository_id == repo_id).scalar()
    if not chunk_total:
        response.headers["X-Index-Job-Id"] = str(job.id)
        return []  # indexed successfully, but the repository has no Python chunks

    try:
        hits = vector_store.search(repo_id, job.id, req.query, req.top_k)
    except EmbeddingError as e:
        raise HTTPException(status_code=503, detail=f"Embedding model unavailable: {sanitize_error(e)}")
    except vector_store.VectorStoreError as e:
        raise HTTPException(status_code=503, detail=f"Vector index unavailable: {sanitize_error(e)}")

    ids = [cid for cid, _ in hits]
    rows = {
        c.id: c
        for c in db.query(CodeChunk).filter(CodeChunk.repository_id == repo_id, CodeChunk.id.in_(ids))
    } if ids else {}

    results: List[SearchResult] = []
    for cid, score in hits:
        row = rows.get(cid)
        if row is None:
            logger.warning("Vector hit %s has no matching chunk row in repo %s", cid, repo_id)
            continue
        results.append(
            SearchResult(
                id=row.id,
                repository_id=row.repository_id,
                file_path=row.file_path,
                chunk_index=row.chunk_index,
                chunk_type=row.chunk_type,
                symbol_name=row.symbol_name,
                start_line=row.start_line,
                end_line=row.end_line,
                content=row.content,
                score=score,
            )
        )

    if hits and not results:
        raise HTTPException(
            status_code=503,
            detail="Vector index is out of sync with the database; re-run ingestion for this repository.",
        )

    response.headers["X-Index-Job-Id"] = str(job.id)
    return results
