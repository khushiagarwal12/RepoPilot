"""Alembic against a REAL PostgreSQL. Runs only when TEST_POSTGRES_URL is set (CI sets it).

SAFETY: this test drops and recreates the `public` schema, so it refuses to run unless the
database name contains "test".
"""
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

URL = os.getenv("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_POSTGRES_URL not set")
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def pg(monkeypatch):
    engine = create_engine(URL)
    assert "test" in (engine.url.database or ""), "refusing to touch a non-test database"
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    monkeypatch.setenv("POSTGRES_URL", URL)
    monkeypatch.setenv("REPOPILOT_ENV_FILE", str(ROOT / "does-not-exist.env"))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    yield engine, cfg
    engine.dispose()


def test_upgrade_downgrade_upgrade_cycle_and_no_drift(pg):
    engine, cfg = pg
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")   # regression: used to leave the job_status enum behind
    command.upgrade(cfg, "head")

    os.environ.setdefault("REPOPILOT_ALLOW_SQLITE", "1")
    from backend.database import Base
    import backend.models  # noqa: F401
    with engine.connect() as c:
        assert compare_metadata(MigrationContext.configure(c), Base.metadata) == []


def test_database_allows_only_one_active_job_per_repository(pg):
    from sqlalchemy.exc import IntegrityError
    engine, cfg = pg
    command.upgrade(cfg, "head")
    with engine.begin() as c:
        c.execute(text("INSERT INTO repositories (github_url, name, active) VALUES ('u', 'n', true)"))
        rid = c.execute(text("SELECT id FROM repositories")).scalar()
        c.execute(text("INSERT INTO ingestion_jobs (repository_id, status) VALUES (:r, 'running')"), {"r": rid})
        c.execute(text("INSERT INTO ingestion_jobs (repository_id, status) VALUES (:r, 'succeeded')"), {"r": rid})
    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            c.execute(text("INSERT INTO ingestion_jobs (repository_id, status) VALUES (:r, 'pending')"), {"r": rid})
