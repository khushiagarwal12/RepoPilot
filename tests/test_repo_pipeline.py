import os
import shutil
import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base, get_db
import backend.routers.repo as repo_router
from backend.main import app
from backend.models import Repository, IngestionJob, CodeDefinition, CodeChunk

# Setup a clean in-memory/file SQLite test DB
TEST_DB_FILE = "test_repo_pipeline.db"
SQLALCHEMY_DATABASE_URL = f"sqlite:///./{TEST_DB_FILE}"

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def client():
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    if os.path.exists(TEST_DB_FILE):
        try:
            os.remove(TEST_DB_FILE)
        except OSError:
            pass


def test_ingestion_and_definition_parsing_pipeline(client, monkeypatch, tmp_path):
    # Prepare dummy extracted repo contents
    mock_extracted_dir = tmp_path / "mock_repo"
    mock_extracted_dir.mkdir()
    (mock_extracted_dir / "math_utils.py").write_text(
        "import math\n\ndef add(a, b):\n    return a + b\n\nclass Calculator:\n    def compute(self):\n        pass\n",
        encoding="utf-8"
    )

    # Monkeypatch github download to avoid hitting external network
    def mock_download_and_extract(github_url, target_dir):
        return str(mock_extracted_dir)

    monkeypatch.setattr(
        "backend.routers.repo.download_and_extract_repo",
        mock_download_and_extract
    )

    # Save original function and run with TestingSessionLocal
    orig_process_ingestion = repo_router.process_repository_ingestion

    def mock_process_ingestion(repo_id, job_id, github_url, **kwargs):
        orig_process_ingestion(
            repo_id, job_id, github_url, db_factory=TestingSessionLocal
        )

    monkeypatch.setattr(
        repo_router,
        "process_repository_ingestion",
        mock_process_ingestion
    )

    # Test POST /repositories/
    post_res = client.post("/repositories/", json={"github_url": "https://github.com/example/math-lib"})
    assert post_res.status_code == 200
    data = post_res.json()
    assert "repository_id" in data
    assert "job_id" in data
    repo_id = data["repository_id"]

    # Check job status
    db = TestingSessionLocal()
    job = db.query(IngestionJob).filter(IngestionJob.id == data["job_id"]).first()
    assert job.status == "succeeded"
    db.close()

    # Test GET /repositories/{repo_id}/definitions
    defs_res = client.get(f"/repositories/{repo_id}/definitions")
    assert defs_res.status_code == 200
    defs = defs_res.json()

    symbol_names = {d["name"] for d in defs}
    assert "add" in symbol_names
    assert "Calculator" in symbol_names

    # Test filter by definition_type
    func_res = client.get(f"/repositories/{repo_id}/definitions?definition_type=function")
    assert func_res.status_code == 200
    funcs = func_res.json()
    assert all(f["definition_type"] == "function" for f in funcs)
    assert {f["name"] for f in funcs} == {"add"}

    # Test GET /repositories/{repo_id}/chunks
    chunks_res = client.get(f"/repositories/{repo_id}/chunks")
    assert chunks_res.status_code == 200
    chunks = chunks_res.json()
    assert len(chunks) >= 2
    chunk_types = {c["chunk_type"] for c in chunks}
    assert "function" in chunk_types or "class" in chunk_types or "module_level" in chunk_types
