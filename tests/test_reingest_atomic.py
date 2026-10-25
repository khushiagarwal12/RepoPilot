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

# Use a test DB file dedicated to atomic tests
TEST_DB_FILE = "test_atomic_reingest.db"
SQLALCHEMY_DATABASE_URL = f"sqlite:///./{TEST_DB_FILE}"

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def client_atomic():
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


def test_atomic_rollback_on_reingest_failure(client_atomic, monkeypatch, tmp_path):
    """Verifies that if re-ingestion encounters an unexpected error,

    the previous valid definitions and chunks remain intact.
    """
    mock_extracted_dir = tmp_path / "mock_v1"
    mock_extracted_dir.mkdir()
    (mock_extracted_dir / "mod.py").write_text("def v1_func(): pass\n", encoding="utf-8")

    def mock_download_v1(github_url, target_dir):
        return str(mock_extracted_dir)

    monkeypatch.setattr(repo_router, "download_and_extract_repo", mock_download_v1)

    orig_process = repo_router.process_repository_ingestion

    def mock_process_ingestion(repo_id, job_id, github_url, **kwargs):
        orig_process(repo_id, job_id, github_url, db_factory=TestingSessionLocal)

    monkeypatch.setattr(repo_router, "process_repository_ingestion", mock_process_ingestion)

    # 1. Initial successful ingestion
    res1 = client_atomic.post("/repositories/", json={"github_url": "https://github.com/test-org/atomic-repo"})
    assert res1.status_code == 200
    repo_id = res1.json()["repository_id"]

    defs_res1 = client_atomic.get(f"/repositories/{repo_id}/definitions")
    assert any(d["name"] == "v1_func" for d in defs_res1.json())

    # 2. Simulate failure during re-ingestion parsing
    def broken_parse(repo_root):
        raise RuntimeError("Simulated crash during AST parsing")

    monkeypatch.setattr(repo_router, "parse_repository", broken_parse)

    res2 = client_atomic.post("/repositories/", json={"github_url": "https://github.com/test-org/atomic-repo"})
    assert res2.status_code == 200

    # Verify that job failed
    jobs_res = client_atomic.get(f"/repositories/{repo_id}/jobs")
    latest_job = jobs_res.json()[0]
    assert latest_job["status"] == "failed"
    assert "Simulated crash" in latest_job["log"]

    # CRUCIAL: Previous definitions must NOT have been wiped!
    defs_after_fail = client_atomic.get(f"/repositories/{repo_id}/definitions").json()
    assert len(defs_after_fail) > 0
    assert any(d["name"] == "v1_func" for d in defs_after_fail)
