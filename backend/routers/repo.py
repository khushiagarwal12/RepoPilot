import logging
import os
import shutil
import time
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import vector_store
from backend.ask import SNIPPET_CHARS, answer_question
from backend.chunker.python_chunker import chunk_repository
from backend.config import get_ask_min_score, get_repo_data_dir
from backend.database import SessionLocal, get_db
from backend.llm import LLMClient, LLMError, get_llm_client
from backend.models import CodeChunk, CodeDefinition, IngestionJob, Repository
from backend.parser.python_ast import parse_repository
from backend.retrieval import IndexUnavailableError, NotIndexedError, retrieve
from backend.ratelimit import rate_limit
from backend.schemas import (
    JobBrief,
    RepositorySummary,
    AskRequest,
    AskResponse,
    Citation,
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
        logger.exception("Ingestion job %s failed during %s", job_id, stage)
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


@router.get("/", response_model=List[RepositorySummary])
def list_repositories(db: Session = Depends(get_db)):
    """Most recent repositories (max 100) with their latest job status."""
    out = []
    for repo in db.query(Repository).order_by(Repository.id.desc()).limit(100).all():
        jobs = db.query(IngestionJob).filter(IngestionJob.repository_id == repo.id).order_by(IngestionJob.id.desc()).all()
        out.append(RepositorySummary(
            id=repo.id, name=repo.name, github_url=repo.github_url,
            latest_job=JobBrief(id=jobs[0].id, status=jobs[0].status) if jobs else None,
            indexed=any(j.status == "succeeded" for j in jobs),
        ))
    return out


@router.post("/", response_model=IngestResponse, dependencies=[Depends(rate_limit("ingest"))])
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


def _search_result(row: CodeChunk, score: float) -> SearchResult:
    return SearchResult(
        id=row.id, repository_id=row.repository_id, file_path=row.file_path, chunk_index=row.chunk_index,
        chunk_type=row.chunk_type, symbol_name=row.symbol_name, start_line=row.start_line,
        end_line=row.end_line, content=row.content, score=score,
    )


def _retrieve_or_http_error(db: Session, repo_id: int, query: str, top_k: int, mode=None):
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    try:
        return retrieve(db, repo_id, query, top_k, mode)
    except NotIndexedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except IndexUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/{repo_id}/search", response_model=List[SearchResult], dependencies=[Depends(rate_limit("query"))])
def search_repository(repo_id: int, req: SearchRequest, response: Response, db: Session = Depends(get_db)):
    """Semantic search over the code chunks of ONE repository (latest succeeded index).

    Errors are explicit, never an empty list that hides a failure:
    404 unknown repository; 409 not indexed yet; 503 embedding model or vector index unavailable / out of sync.
    """
    result = _retrieve_or_http_error(db, repo_id, req.query, req.top_k, req.mode)
    response.headers["X-Index-Job-Id"] = str(result.job_id)
    return [_search_result(row, score) for row, score in result.hits]


@router.post("/{repo_id}/ask", response_model=AskResponse, dependencies=[Depends(rate_limit("query"))])
def ask_repository(repo_id: int, req: AskRequest, db: Session = Depends(get_db),
                   llm: LLMClient = Depends(get_llm_client)):
    """Answer a question using only retrieved chunks, with verifiable citations.

    ``insufficient_evidence`` is returned (HTTP 200) when retrieval finds nothing relevant
    enough, when the model says the sources do not answer the question, or when its answer
    carries no valid citation. 404/409/503 as for /search; 503 also when the LLM fails.
    """
    started = time.monotonic()
    retrieval = _retrieve_or_http_error(db, repo_id, req.question, req.top_k)
    try:
        result = answer_question(req.question, retrieval, llm, get_ask_min_score())
    except LLMError as e:
        logger.warning("ask repo=%s llm_error=%s", repo_id, type(e).__name__)
        raise HTTPException(status_code=503, detail=f"LLM unavailable: {sanitize_error(e)}")

    citations = [
        Citation(
            n=n, chunk_id=result.sources[n - 1][0].id, file_path=result.sources[n - 1][0].file_path,
            start_line=result.sources[n - 1][0].start_line, end_line=result.sources[n - 1][0].end_line,
            snippet=(result.sources[n - 1][0].content or "")[:SNIPPET_CHARS], score=result.sources[n - 1][1],
        )
        for n in result.cited
    ]
    logger.info("ask repo=%s hits=%d status=%s cited=%d latency_ms=%d", repo_id, len(retrieval.hits),
                result.status, len(citations), (time.monotonic() - started) * 1000)
    return AskResponse(
        status=result.status, answer=result.answer, citations=citations, index_job_id=retrieval.job_id,
        model=getattr(llm, "model", None) if result.status == "answered" else None,
    )
