from pydantic import BaseModel, HttpUrl, ConfigDict
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

# New models for semantic search
class SearchRequest(BaseModel):
    query: str
    top_k: int = 5

class SearchResult(BaseModel):
    file_path: str
    name: str
    type: str
    score: float
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
