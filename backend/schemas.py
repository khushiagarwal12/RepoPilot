from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator
from typing import Optional, List
from datetime import datetime


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
