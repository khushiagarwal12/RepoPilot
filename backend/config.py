"""Central, cwd-independent configuration helpers.

Paths are anchored to the project root (the folder that contains ``backend/``),
so they do not change depending on which directory Uvicorn is started from.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_env_file() -> Path:
    """The ``.env`` file that is loaded: ``REPOPILOT_ENV_FILE`` or ``<project root>/.env``.

    The location never depends on the current working directory.
    """
    override = os.getenv("REPOPILOT_ENV_FILE")
    return Path(override).expanduser() if override else PROJECT_ROOT / ".env"


def load_env() -> Optional[Path]:
    """Load the environment file (if it exists) into ``os.environ``.

    Existing environment variables win over file values. Contents are never
    logged. Returns the path that was loaded, or ``None`` if there is no file.
    A read failure raises a RuntimeError naming the file and the OS error, so a
    problem such as ``[Errno 9]`` is reported with context instead of a bare
    traceback from deep inside another library.
    """
    path = get_env_file()
    if not path.is_file():
        return None
    try:
        load_dotenv(dotenv_path=path, override=False)
    except OSError as exc:
        raise RuntimeError(
            f"Could not read environment file '{path}': "
            f"{type(exc).__name__} (errno {exc.errno}) {exc.strerror}"
        ) from exc
    return path


def _resolve(path_str: str) -> Path:
    p = Path(path_str).expanduser()
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


def get_chroma_path() -> str:
    """Directory for ChromaDB's persistent data (``CHROMA_DB_PATH`` or ``<root>/chroma_store``)."""
    return str(_resolve(os.getenv("CHROMA_DB_PATH") or "chroma_store"))


def get_repo_data_dir() -> Path:
    """Directory where extracted repositories are kept (``<root>/repo_data``)."""
    return PROJECT_ROOT / "repo_data"


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def get_ask_min_score() -> float:
    """Minimum similarity of the BEST retrieved chunk for /ask to call the LLM (uncalibrated default)."""
    return _float_env("ASK_MIN_SCORE", 0.20)


def get_llm_timeout() -> float:
    return _float_env("LLM_TIMEOUT_SECONDS", 45.0)


def get_llm_model() -> str:
    return os.getenv("LLM_MODEL") or "gemini-2.5-flash"
