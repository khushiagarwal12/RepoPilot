"""Environment loading, database selection and import-time side effects."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import backend.config as config

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run(code, tmp_path, extra_env=None, drop=("POSTGRES_URL", "REPOPILOT_ALLOW_SQLITE", "CHROMA_DB_PATH")):
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["REPOPILOT_ENV_FILE"] = str(tmp_path / "does_not_exist.env")  # never read the developer's real .env
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-W", "ignore", "-c", code], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=120)


# ------------------------------------------------------------------ database selection

def test_missing_postgres_url_fails_loudly_instead_of_using_sqlite(tmp_path):
    r = _run("import backend.database", tmp_path)
    assert r.returncode != 0
    assert "POSTGRES_URL is not set" in r.stderr and "REPOPILOT_ALLOW_SQLITE" in r.stderr
    assert not (tmp_path / "repopilot.db").exists()


def test_sqlite_requires_explicit_opt_in_and_is_anchored_to_project_root(tmp_path):
    r = _run("import backend.database as d; print(d.engine.url.drivername, d.engine.url.database)",
             tmp_path, {"REPOPILOT_ALLOW_SQLITE": "1"})
    assert r.returncode == 0, r.stderr
    driver, db_path = r.stdout.strip().split(maxsplit=1)
    assert driver == "sqlite"
    assert Path(db_path) == PROJECT_ROOT / "repopilot.db"   # not relative to the cwd (tmp_path)


def test_postgres_url_is_used_when_set(tmp_path):
    r = _run("import backend.database as d; print(d.engine.url.drivername, d.engine.url.host, d.engine.url.database)",
             tmp_path, {"POSTGRES_URL": "postgresql://u:pw@db.example:5432/mydb"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["postgresql", "db.example", "mydb"]


def test_missing_database_message_never_contains_credentials(tmp_path):
    r = _run("import backend.database", tmp_path, {"REPOPILOT_ENV_FILE": str(tmp_path / "x.env")})
    assert r.returncode != 0 and "POSTGRES_URL is not set" in r.stderr
    assert "hunter2" not in r.stdout + r.stderr


# ------------------------------------------------------------------ .env loading

def test_load_env_uses_explicit_file_regardless_of_cwd(tmp_path, monkeypatch):
    env_file = tmp_path / "custom.env"
    env_file.write_text("REPOPILOT_TEST_VALUE=from-file\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("REPOPILOT_TEST_VALUE=WRONG-cwd-file\n", encoding="utf-8")

    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("REPOPILOT_ENV_FILE", str(env_file))
    monkeypatch.delenv("REPOPILOT_TEST_VALUE", raising=False)

    assert config.load_env() == env_file
    assert os.environ["REPOPILOT_TEST_VALUE"] == "from-file"
    monkeypatch.delenv("REPOPILOT_TEST_VALUE")


def test_real_environment_variables_win_over_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "x.env"
    env_file.write_text("REPOPILOT_TEST_VALUE=from-file\n", encoding="utf-8")
    monkeypatch.setenv("REPOPILOT_ENV_FILE", str(env_file))
    monkeypatch.setenv("REPOPILOT_TEST_VALUE", "from-shell")
    config.load_env()
    assert os.environ["REPOPILOT_TEST_VALUE"] == "from-shell"


def test_missing_env_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOPILOT_ENV_FILE", str(tmp_path / "nope.env"))
    assert config.load_env() is None


def test_env_read_failure_is_reported_with_context_and_without_contents(tmp_path, monkeypatch):
    env_file = tmp_path / "s.env"
    env_file.write_text("SECRET_TOKEN=super-secret-value\n", encoding="utf-8")
    monkeypatch.setenv("REPOPILOT_ENV_FILE", str(env_file))

    def boom(*a, **k):
        raise OSError(9, "Bad file descriptor")

    monkeypatch.setattr(config, "load_dotenv", boom)
    with pytest.raises(RuntimeError) as exc:
        config.load_env()
    msg = str(exc.value)
    assert "errno 9" in msg and "Bad file descriptor" in msg and "s.env" in msg
    assert "super-secret-value" not in msg


def test_paths_are_anchored_to_project_root_not_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("CHROMA_DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    assert Path(config.get_chroma_path()) == PROJECT_ROOT / "chroma_store"
    assert config.get_repo_data_dir() == PROJECT_ROOT / "repo_data"
    monkeypatch.setenv("CHROMA_DB_PATH", "rel/dir")
    assert Path(config.get_chroma_path()) == (PROJECT_ROOT / "rel" / "dir").resolve()


# ------------------------------------------------------------------ ChromaDB vs .env / import side effects

def test_importing_the_app_has_no_filesystem_side_effects(tmp_path):
    chroma_dir = tmp_path / "never_created"
    r = _run("import backend.main", tmp_path,
             {"REPOPILOT_ALLOW_SQLITE": "1", "CHROMA_DB_PATH": str(chroma_dir)})
    assert r.returncode == 0, r.stderr
    assert not chroma_dir.exists()                       # Chroma client is lazy (was created at import)
    assert not (tmp_path / "chroma").exists()
    assert not (tmp_path / "repopilot.db").exists()


def test_chromadb_does_not_read_dotenv_from_the_working_directory(tmp_path, monkeypatch):
    """Regression: chromadb.Settings reads ./.env by default; our client must not."""
    from backend import vector_store

    (tmp_path / ".env").write_text("CHROMA_SERVER_HOST=leaked-from-dotenv\nOPENAI_API_KEY=fake\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    client = vector_store._get_client()
    assert client.get_settings().chroma_server_host is None


def test_gitignore_covers_local_chroma_and_sqlite_data():
    lines = {l.strip() for l in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert {"chroma/", "chroma_store/", "repo_data/", ".env", "*.db"} <= lines
