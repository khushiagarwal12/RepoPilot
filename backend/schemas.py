from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class RepositoryCreate(BaseModel):
    github_url: HttpUrl


class RepositoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    github_url: str
    name: str
    active: bool
    created_at: Optional[datetime] = None


class IngestionJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int
    status: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    log: Optional[str] = None


class IngestResponse(BaseModel):
    repository_id: int
    job_id: int
    message: str


class CodeDefinitionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int
    file_path: str
    name: str
    definition_type: str
    start_line: int
    end_line: int


class CodeChunkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repository_id: int
    file_path: str
    chunk_index: int
    chunk_type: str
    symbol_name: Optional[str] = None
    start_line: int
    end_line: int
    content: str

# --- Semantic search ---------------------------------------------------------

class SearchRequest(BaseModel):
    mode: Optional[Literal["vector", "hybrid"]] = Field(None, description="Override RETRIEVAL_MODE (for evaluation)")
    query: str = Field(..., min_length=1, max_length=2000, description="Natural-language or code query")
    top_k: int = Field(5, ge=1, le=50, description="Number of results to return (1-50)")

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query must not be blank")
        return v


class SearchResult(CodeChunkResponse):
    """A persisted code chunk plus its similarity score (1.0 = most similar)."""

    score: float


# --- Grounded question answering ---------------------------------------------

class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(8, ge=1, le=20, description="Number of chunks retrieved as evidence (1-20)")

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question must not be blank")
        return v


class Citation(BaseModel):
    n: int
    chunk_id: int
    file_path: str
    start_line: int
    end_line: int
    snippet: str
    score: float


class AskResponse(BaseModel):
    status: str  # "answered" | "insufficient_evidence"
    answer: Optional[str] = None
    citations: List[Citation] = []
    index_job_id: int
    model: Optional[str] = None


class JobBrief(BaseModel):
    id: int
    status: str


class RepositorySummary(BaseModel):
    id: int
    name: str
    github_url: str
    latest_job: Optional[JobBrief] = None
    indexed: bool  # True only if a job has succeeded
