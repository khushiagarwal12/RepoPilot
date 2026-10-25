import logging
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from backend.config import PROJECT_ROOT, load_env

load_env()

logger = logging.getLogger(__name__)


def _resolve_database_url() -> str:
    """Pick the database explicitly.

    * ``POSTGRES_URL`` set  -> use it (normal development/production).
    * otherwise, only if ``REPOPILOT_ALLOW_SQLITE`` is 1/true/yes -> a local
      throwaway SQLite file in the project folder (clearly logged).
    * otherwise -> fail at startup with instructions. The app never silently
      connects to a different database than the one you configured.
    """
    url = os.getenv("POSTGRES_URL")
    if url:
        return url
    if os.getenv("REPOPILOT_ALLOW_SQLITE", "").strip().lower() in ("1", "true", "yes"):
        logger.warning("POSTGRES_URL is not set; using a local SQLite file (development only).")
        return f"sqlite:///{(PROJECT_ROOT / 'repopilot.db').as_posix()}"
    raise RuntimeError(
        "POSTGRES_URL is not set. Copy .env.example to .env and set POSTGRES_URL, "
        "or set REPOPILOT_ALLOW_SQLITE=1 to use a throwaway local SQLite database."
    )


DATABASE_URL = _resolve_database_url()

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
