import io
import os
import stat
import zipfile
import pytest
from backend.utils.github import parse_github_url, safe_extract_zip


def test_parse_github_url_valid():
    owner, repo = parse_github_url("https://github.com/pallets/flask")
    assert owner == "pallets"
    assert repo == "flask"

    owner, repo = parse_github_url("https://github.com/psf/requests.git")
    assert owner == "psf"
    assert repo == "requests"


def test_parse_github_url_invalid():
    with pytest.raises(ValueError):
        parse_github_url("https://gitlab.com/foo/bar")

    with pytest.raises(ValueError):
        parse_github_url("not_a_url")

    with pytest.raises(ValueError):
        parse_github_url("https://github.com/lonely-owner")


def _zip_bytes(entries):
    """entries: list of (name_or_ZipInfo, data)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            z.writestr(name, data)
    buf.seek(0)
    return buf


def _unix_entry(name, mode):
    zinfo = zipfile.ZipInfo(name)
    zinfo.create_system = 3  # Unix
    zinfo.external_attr = mode << 16
    return zinfo


def _leftovers(tmp_path, target_name="target"):
    """Anything in tmp_path other than the (untouched) target directory."""
    return [p.name for p in tmp_path.iterdir() if p.name != target_name]


def test_safe_extract_zip_extracts_normal_files(tmp_path):
    target = tmp_path / "target"
    buf = _zip_bytes([
        ("repo-abc/README.md", "hello"),
        ("repo-abc/src/app.py", "print('x')\n"),
        ("repo-abc/empty_dir/", ""),
    ])
    with zipfile.ZipFile(buf) as z:
        result = safe_extract_zip(z, str(target))

    assert (target / "repo-abc" / "README.md").read_text() == "hello"
    assert (target / "repo-abc" / "src" / "app.py").read_text() == "print('x')\n"
    assert (target / "repo-abc" / "empty_dir").is_dir()
    assert result["files"] == 2
    assert result["skipped_symlinks"] == []
    assert _leftovers(tmp_path) == []  # no staging directory left behind


def test_safe_extract_zip_skips_symlinks_but_extracts_regular_files(tmp_path):
    """Regression: GitHub archives (e.g. psf/requests) contain symlink entries."""
    target = tmp_path / "target"
    link = _unix_entry("repo-abc/tests/certs/mtls/client/ca", stat.S_IFLNK | 0o777)
    buf = _zip_bytes([
        ("repo-abc/README.md", "hello"),
        (link, "../../../ca"),
        ("repo-abc/setup.py", "x = 1\n"),
    ])
    with zipfile.ZipFile(buf) as z:
        result = safe_extract_zip(z, str(target))

    assert (target / "repo-abc" / "README.md").exists()
    assert (target / "repo-abc" / "setup.py").exists()
    link_path = target / "repo-abc" / "tests" / "certs" / "mtls" / "client" / "ca"
    assert not os.path.lexists(link_path)  # neither a link nor a file was created
    assert result["skipped_symlinks"] == ["repo-abc/tests/certs/mtls/client/ca"]
    assert result["files"] == 2


def test_symlink_target_is_never_followed_or_written(tmp_path):
    """A symlink entry pointing outside must not cause any write outside."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "target"
    link = _unix_entry("repo/evil", stat.S_IFLNK | 0o777)
    buf = _zip_bytes([
        (link, str(outside)),
        ("repo/evil/pwned.txt", "payload"),  # would land in `outside` if a link existed
    ])
    with zipfile.ZipFile(buf) as z:
        safe_extract_zip(z, str(target))

    assert list(outside.iterdir()) == []
    assert (target / "repo" / "evil" / "pwned.txt").read_text() == "payload"


def test_safe_extract_zip_prevents_path_traversal(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    buf = _zip_bytes([("ok.txt", "fine"), ("../evil.txt", "malicious payload")])

    with zipfile.ZipFile(buf, "r") as z:
        with pytest.raises(ValueError, match="Malicious archive detected"):
            safe_extract_zip(z, str(target_dir))

    assert not (tmp_path / "evil.txt").exists()


def test_safe_extract_zip_prevents_nested_traversal_and_backslashes(tmp_path):
    for evil in ("a/../../evil.txt", "..\\evil.txt", "a\\..\\..\\evil.txt"):
        target_dir = tmp_path / "t"
        buf = _zip_bytes([(evil, "x")])
        with zipfile.ZipFile(buf, "r") as z:
            with pytest.raises(ValueError, match="Malicious archive detected"):
                safe_extract_zip(z, str(target_dir))
        assert not (tmp_path / "evil.txt").exists()


def test_safe_extract_zip_prevents_absolute_paths(tmp_path):
    for name in ("/etc/passwd", "C:/Windows/evil.dll", "c:\\evil.dll"):
        target_dir = tmp_path / "target"
        buf = _zip_bytes([(name, "payload")])
        with zipfile.ZipFile(buf, "r") as z:
            with pytest.raises(ValueError, match="absolute path"):
                safe_extract_zip(z, str(target_dir))


def test_safe_extract_zip_rejects_device_entries(tmp_path):
    target = tmp_path / "target"
    dev = _unix_entry("repo/dev/null", stat.S_IFCHR | 0o666)
    buf = _zip_bytes([("repo/ok.txt", "fine"), (dev, "")])
    with zipfile.ZipFile(buf) as z:
        with pytest.raises(ValueError, match="special file"):
            safe_extract_zip(z, str(target))
    assert not target.exists()


def test_safe_extract_zip_prevents_decompression_bomb(tmp_path, monkeypatch):
    target_dir = tmp_path / "target"
    target_dir.mkdir()

    import backend.utils.github as gh_utils
    monkeypatch.setattr(gh_utils, "MAX_UNCOMPRESSED_BYTES", 100)

    buf = _zip_bytes([("big.txt", "A" * 200)])
    with zipfile.ZipFile(buf, "r") as z:
        with pytest.raises(ValueError, match="exceeds limit"):
            safe_extract_zip(z, str(target_dir))


def test_actual_bytes_limit_enforced_when_zip_headers_lie(tmp_path, monkeypatch):
    """Declared sizes are untrusted: the streamed byte count must be enforced."""
    import backend.utils.github as gh_utils
    monkeypatch.setattr(gh_utils, "MAX_UNCOMPRESSED_BYTES", 100)

    buf = _zip_bytes([("a.txt", "A" * 80), ("b.txt", "B" * 80)])
    target = tmp_path / "target"
    with zipfile.ZipFile(buf) as z:
        for info in z.infolist():
            info.file_size = 10  # lie: claim 10 bytes each (20 total < 100)
        # zipfile itself stops at the declared size and fails the CRC check;
        # our streaming byte counter is the second line of defence.
        with pytest.raises((ValueError, zipfile.BadZipFile)):
            safe_extract_zip(z, str(target))
    assert not target.exists()
    assert _leftovers(tmp_path, "target") == []


def test_safe_extract_zip_prevents_excessive_entries(tmp_path, monkeypatch):
    target_dir = tmp_path / "target"
    target_dir.mkdir()

    import backend.utils.github as gh_utils
    monkeypatch.setattr(gh_utils, "MAX_FILE_COUNT", 5)

    buf = _zip_bytes([(f"file_{i}.txt", "content") for i in range(10)])
    with zipfile.ZipFile(buf, "r") as z:
        with pytest.raises(ValueError, match="too many entries"):
            safe_extract_zip(z, str(target_dir))


def test_rejected_archive_leaves_no_partial_output(tmp_path):
    """Valid files listed BEFORE a malicious entry must not be left behind."""
    target = tmp_path / "target"
    buf = _zip_bytes([
        ("repo/a.py", "a = 1\n"),
        ("repo/b.py", "b = 2\n"),
        ("../../escape.txt", "boom"),
    ])
    with zipfile.ZipFile(buf) as z:
        with pytest.raises(ValueError):
            safe_extract_zip(z, str(target))

    assert not target.exists()
    assert _leftovers(tmp_path, "target") == []


def test_failure_midway_through_streaming_cleans_staging(tmp_path, monkeypatch):
    import backend.utils.github as gh_utils
    monkeypatch.setattr(gh_utils, "MAX_UNCOMPRESSED_BYTES", 150)

    buf = _zip_bytes([("a.txt", "A" * 100), ("b.txt", "B" * 100)])  # 200 total > 150
    target = tmp_path / "target"
    target.mkdir()
    with zipfile.ZipFile(buf) as z:
        with pytest.raises(ValueError, match="exceeds limit"):
            safe_extract_zip(z, str(target))
    assert list(target.iterdir()) == []  # pre-existing empty dir untouched
    assert _leftovers(tmp_path) == []


def test_non_empty_target_is_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "old.txt").write_text("old")
    buf = _zip_bytes([("new.txt", "new")])
    with zipfile.ZipFile(buf) as z:
        with pytest.raises(ValueError, match="not empty"):
            safe_extract_zip(z, str(target))
    assert (target / "old.txt").read_text() == "old"
