"""Stored file paths must be repo-root-relative, '/'-separated and cwd-independent."""
import os

import pytest

from backend.chunker.python_chunker import chunk_file, chunk_repository
from backend.parser.python_ast import extract_definitions, parse_repository


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "psf-requests-abc123"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "top.py").write_text("X = 1\n\nclass C:\n    pass\n", encoding="utf-8")
    return root


def test_parser_paths_are_repo_relative(repo):
    files = {d["file"] for d in parse_repository(str(repo))}
    assert files == {"src/pkg/mod.py", "top.py"}


def test_chunker_paths_are_repo_relative(repo):
    files = {c["file_path"] for c in chunk_repository(str(repo))}
    assert files == {"src/pkg/mod.py", "top.py"}


@pytest.mark.parametrize("cwd_kind", ["repo_parent", "unrelated", "root_of_fs"])
def test_paths_do_not_depend_on_working_directory(repo, tmp_path, monkeypatch, cwd_kind):
    cwd = {"repo_parent": repo.parent, "unrelated": tmp_path, "root_of_fs": os.path.abspath(os.sep)}[cwd_kind]
    monkeypatch.chdir(cwd)
    assert {d["file"] for d in parse_repository(str(repo))} == {"src/pkg/mod.py", "top.py"}
    assert {c["file_path"] for c in chunk_repository(str(repo))} == {"src/pkg/mod.py", "top.py"}


def test_paths_never_contain_backslashes_or_dotdot(repo):
    for p in [d["file"] for d in parse_repository(str(repo))] + [c["file_path"] for c in chunk_repository(str(repo))]:
        assert "\\" not in p and ".." not in p.split("/") and not p.startswith("/")


def test_single_file_use_without_root_is_cwd_independent(repo, tmp_path, monkeypatch):
    f = str(repo / "src" / "pkg" / "mod.py")
    monkeypatch.chdir(tmp_path)
    assert extract_definitions(f)[0]["file"] == "mod.py"
    assert chunk_file(f)[0]["file_path"] == "mod.py"
    assert chunk_file(f, repo_root=str(repo))[0]["file_path"] == "src/pkg/mod.py"


def test_unreadable_python_files_are_skipped_not_fatal(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    (root / "ok.py").write_bytes(b"def ok():\n    return 1\n")
    (root / "bad_encoding.py").write_bytes(b"\xff\xfe# not utf-8\ndef x(): pass\n")
    (root / "nullbyte.py").write_bytes(b"x = 1\x00\n")
    (root / "broken.py").write_bytes(b"def broken(:\n")

    defs = parse_repository(str(root))
    assert [d["name"] for d in defs] == ["ok"]
    chunks = chunk_repository(str(root))
    assert any(c["symbol_name"] == "ok" for c in chunks)
