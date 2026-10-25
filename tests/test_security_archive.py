import io
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


def test_safe_extract_zip_prevents_path_traversal(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()

    # Construct malicious zip with ../../../evil.txt
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.txt", "malicious payload")
    buf.seek(0)

    with zipfile.ZipFile(buf, "r") as z:
        with pytest.raises(ValueError, match="Malicious archive detected"):
            safe_extract_zip(z, str(target_dir))


def test_safe_extract_zip_prevents_absolute_paths(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()

    # Unix absolute path
    buf1 = io.BytesIO()
    with zipfile.ZipFile(buf1, "w") as z:
        z.writestr("/etc/passwd", "root:x:0:0:")
    buf1.seek(0)
    with zipfile.ZipFile(buf1, "r") as z:
        with pytest.raises(ValueError, match="absolute path"):
            safe_extract_zip(z, str(target_dir))

    # Windows drive prefix
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w") as z:
        z.writestr("C:/Windows/evil.dll", "payload")
    buf2.seek(0)
    with zipfile.ZipFile(buf2, "r") as z:
        with pytest.raises(ValueError, match="absolute path"):
            safe_extract_zip(z, str(target_dir))


def test_safe_extract_zip_prevents_symlinks(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        zinfo = zipfile.ZipInfo("symlink_entry")
        # Set Unix symlink mode
        zinfo.create_system = 3  # Unix
        zinfo.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(zinfo, "/etc/shadow")
    buf.seek(0)

    with zipfile.ZipFile(buf, "r") as z:
        with pytest.raises(ValueError, match="symbolic link"):
            safe_extract_zip(z, str(target_dir))


def test_safe_extract_zip_prevents_decompression_bomb(tmp_path, monkeypatch):
    target_dir = tmp_path / "target"
    target_dir.mkdir()

    # Temporarily set max limit low
    import backend.utils.github as gh_utils
    monkeypatch.setattr(gh_utils, "MAX_UNCOMPRESSED_BYTES", 100)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("big.txt", "A" * 200)
    buf.seek(0)

    with zipfile.ZipFile(buf, "r") as z:
        with pytest.raises(ValueError, match="exceeds limit"):
            safe_extract_zip(z, str(target_dir))


def test_safe_extract_zip_prevents_excessive_entries(tmp_path, monkeypatch):
    target_dir = tmp_path / "target"
    target_dir.mkdir()

    import backend.utils.github as gh_utils
    monkeypatch.setattr(gh_utils, "MAX_FILE_COUNT", 5)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(10):
            z.writestr(f"file_{i}.txt", "content")
    buf.seek(0)

    with zipfile.ZipFile(buf, "r") as z:
        with pytest.raises(ValueError, match="too many entries"):
            safe_extract_zip(z, str(target_dir))
