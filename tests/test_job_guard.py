"""One active job per repository (DB-enforced) and recovery of interrupted jobs."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import IngestionJob, Repository
from backend.routers.repo import recover_interrupted_jobs


@pytest.fixture
def Session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'g.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def _repo(db):
    r = Repository(github_url="https://github.com/a/b", name="a/b")
    db.add(r)
    db.commit()
    return r.id


def test_database_rejects_second_active_job(Session):
    db = Session()
    rid = _repo(db)
    db.add(IngestionJob(repository_id=rid, status="running"))
    db.commit()
    db.add(IngestionJob(repository_id=rid, status="pending"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_finished_jobs_do_not_block_new_active_job(Session):
    db = Session()
    rid = _repo(db)
    db.add_all([IngestionJob(repository_id=rid, status="succeeded"),
                IngestionJob(repository_id=rid, status="failed"),
                IngestionJob(repository_id=rid, status="pending")])
    db.commit()  # must not raise


def test_recover_interrupted_jobs_marks_active_jobs_failed(Session):
    db = Session()
    rid = _repo(db)
    db.add_all([IngestionJob(repository_id=rid, status="succeeded"), IngestionJob(repository_id=rid, status="running")])
    db.commit()
    assert recover_interrupted_jobs(Session) == 1
    statuses = sorted(j.status for j in db.query(IngestionJob).all())
    assert statuses == ["failed", "succeeded"]
    assert "restarted" in db.query(IngestionJob).filter_by(status="failed").one().log
    db.add(IngestionJob(repository_id=rid, status="pending"))
    db.commit()  # re-indexing is possible again
    assert recover_interrupted_jobs(Session) == 1
