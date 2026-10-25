"""URL validation and hardened download tests (no real network access)."""
import io
import os
import tempfile
import zipfile

import pytest
import requests

import backend.utils.github as gh


# ------------------------------------------------------------------ URL validation

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/pallets/flask", ("pallets", "flask")),
    ("https://github.com/psf/requests.git", ("psf", "requests")),
    ("https://github.com/psf/requests/", ("psf", "requests")),
    ("https://www.github.com/psf/requests", ("psf", "requests")),
    ("https://GitHub.com/psf/requests", ("psf", "requests")),
    ("https://github.com/some-org/my_repo.v2", ("some-org", "my_repo.v2")),
    ("https://github.com:443/psf/requests", ("psf", "requests")),
])
def test_valid_github_urls(url, expected):
    assert gh.parse_github_url(url) == expected


@pytest.mark.parametrize("url", [
    # look-alike / wrong hosts
    "https://github.com.evil.com/psf/requests",
    "https://evilgithub.com/psf/requests",
    "https://notgithub.com/psf/requests",
    "https://evil.com/github.com/psf/requests",
    "https://github.com@evil.com/psf/requests",
    "https://gitlab.com/foo/bar",
    "https://api.github.com/repos/psf/requests",
    "https://raw.githubusercontent.com/psf/requests",
    "https://localhost/psf/requests",
    "https://127.0.0.1/psf/requests",
    "https://[::1]/psf/requests",
    # scheme problems
    "http://github.com/psf/requests",
    "ftp://github.com/psf/requests",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "//github.com/psf/requests",
    # credentials / ports
    "https://user:pw@github.com/psf/requests",
    "https://github.com:8443/psf/requests",
    "https://github.com:notaport/psf/requests",
    # structure
    "https://github.com/lonely-owner",
    "https://github.com/",
    "https://github.com",
    "https://github.com/psf/requests/tree/main",
    "https://github.com/psf/requests/issues/1",
    # names GitHub does not allow / traversal tricks
    "https://github.com/psf/..",
    "https://github.com/psf/.",
    "https://github.com/../requests",
    "https://github.com/-bad/requests",
    "https://github.com/ba d/requests",
    "https://github.com/psf/re%2fquests",
    "https://github.com/psf/requests%00",
    # junk
    "not_a_url",
    "",
    " https://github.com/psf/requests",
    "https://github.com/psf/requests ",
    None,
    123,
])
def test_invalid_or_malicious_urls_are_rejected(url):
    with pytest.raises(ValueError):
        gh.parse_github_url(url)


def test_api_returns_400_for_lookalike_domain():
    from fastapi.testclient import TestClient
    from backend.main import app

    client = TestClient(app)
    res = client.post("/repositories/", json={"github_url": "https://github.com.evil.com/a/b"})
    assert res.status_code == 400


# ------------------------------------------------------------------ download helpers

def _zip_bytes(files=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in (files or {"psf-requests-abc123/README.md": "hello", "psf-requests-abc123/a.py": "x=1\n"}).items():
            z.writestr(name, data)
    return buf.getvalue()


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None, chunks=None, raise_mid=None):
        self.status_code = status
        self.headers = dict(headers or {})
        self._body = body
        self._chunks = chunks
        self._raise_mid = raise_mid
        self.closed = False
        self.chunks_served = 0

    def iter_content(self, chunk_size=1):
        chunks = self._chunks if self._chunks is not None else [
            self._body[i:i + 1024] for i in range(0, len(self._body), 1024)
        ]
        for i, c in enumerate(chunks):
            if self._raise_mid is not None and i == 1:
                raise self._raise_mid
            self.chunks_served += 1
            yield c

    def close(self):
        self.closed = True


class FakeGet:
    """Scripted requests.get: maps URL -> response (or exception)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        r = self.routes[url]
        if isinstance(r, BaseException):
            raise r
        return r


API = "https://api.github.com/repos/psf/requests/zipball"
CODELOAD = "https://codeload.github.com/psf/requests/legacy.zip/refs/heads/main"
URL = "https://github.com/psf/requests"


@pytest.fixture
def tmp_dirs(tmp_path, monkeypatch):
    """Redirect the temp dir so tests can assert no temp files leak."""
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir))
    return tmpdir, tmp_path / "work" / "incoming"


def _ok_routes(body):
    return {
        API: FakeResponse(302, headers={"Location": CODELOAD}),
        CODELOAD: FakeResponse(200, body=body, headers={"Content-Length": str(len(body))}),
    }


# ------------------------------------------------------------------ download behaviour

def test_successful_download_extracts_and_cleans_temp_file(monkeypatch, tmp_dirs):
    tmpdir, target = tmp_dirs
    get = FakeGet(_ok_routes(_zip_bytes()))
    monkeypatch.setattr(gh.requests, "get", get)

    root = gh.download_and_extract_repo(URL, str(target))

    assert os.path.basename(root) == "psf-requests-abc123"
    assert (target / "psf-requests-abc123" / "README.md").read_text() == "hello"
    assert [u for u, _ in get.calls] == [API, CODELOAD]
    assert list(tmpdir.iterdir()) == []                         # temp zip removed
    for _, kwargs in get.calls:
        assert kwargs["allow_redirects"] is False               # redirects are validated manually
        assert kwargs["stream"] is True
        assert kwargs["timeout"] == (gh.CONNECT_TIMEOUT_SECONDS, gh.READ_TIMEOUT_SECONDS)


def test_redirect_to_non_github_host_is_refused_and_not_requested(monkeypatch, tmp_dirs):
    tmpdir, target = tmp_dirs
    get = FakeGet({API: FakeResponse(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})})
    monkeypatch.setattr(gh.requests, "get", get)

    with pytest.raises(gh.RepositoryDownloadError, match="unexpected location"):
        gh.download_and_extract_repo(URL, str(target))
    assert [u for u, _ in get.calls] == [API]                   # the evil URL was never fetched
    assert not target.exists() and list(tmpdir.iterdir()) == []


@pytest.mark.parametrize("location", [
    "https://evil.com/x.zip",
    "http://codeload.github.com/x.zip",      # downgrade to http
    "https://codeload.github.com.evil.com/x.zip",
    "https://github.com/psf/requests/archive/main.zip",
])
def test_redirect_host_allowlist(monkeypatch, tmp_dirs, location):
    _, target = tmp_dirs
    monkeypatch.setattr(gh.requests, "get", FakeGet({API: FakeResponse(302, headers={"Location": location})}))
    with pytest.raises(gh.RepositoryDownloadError):
        gh.download_and_extract_repo(URL, str(target))


def test_too_many_redirects(monkeypatch, tmp_dirs):
    _, target = tmp_dirs
    loop = FakeResponse(302, headers={"Location": API})
    monkeypatch.setattr(gh.requests, "get", FakeGet({API: loop}))
    with pytest.raises(gh.RepositoryDownloadError, match="Too many redirects"):
        gh.download_and_extract_repo(URL, str(target))


def test_oversized_download_rejected_by_content_length_before_reading_body(monkeypatch, tmp_dirs):
    tmpdir, target = tmp_dirs
    monkeypatch.setattr(gh, "MAX_DOWNLOAD_BYTES", 1000)
    big = FakeResponse(200, body=b"x" * 5000, headers={"Content-Length": "5000"})
    monkeypatch.setattr(gh.requests, "get", FakeGet({API: big}))

    with pytest.raises(gh.RepositoryDownloadError, match="too large"):
        gh.download_and_extract_repo(URL, str(target))
    assert big.chunks_served == 0
    assert list(tmpdir.iterdir()) == [] and not target.exists()


def test_oversized_download_rejected_while_streaming_without_content_length(monkeypatch, tmp_dirs):
    tmpdir, target = tmp_dirs
    monkeypatch.setattr(gh, "MAX_DOWNLOAD_BYTES", 3000)
    endless = FakeResponse(200, chunks=[b"x" * 1024] * 1000)    # would be ~1 MB
    monkeypatch.setattr(gh.requests, "get", FakeGet({API: endless}))

    with pytest.raises(gh.RepositoryDownloadError, match="too large"):
        gh.download_and_extract_repo(URL, str(target))
    assert endless.chunks_served < 10                           # stopped early, not fully read
    assert list(tmpdir.iterdir()) == [] and not target.exists()


def test_total_download_time_limit(monkeypatch, tmp_dirs):
    _, target = tmp_dirs
    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 200.0     # each call "takes" 200s
        return clock["t"]

    monkeypatch.setattr(gh.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(gh.requests, "get", FakeGet({API: FakeResponse(200, chunks=[b"x"] * 10)}))
    with pytest.raises(gh.RepositoryDownloadError, match="time limit"):
        gh.download_and_extract_repo(URL, str(target))


def test_connection_timeout_is_reported(monkeypatch, tmp_dirs):
    _, target = tmp_dirs
    monkeypatch.setattr(gh.requests, "get", FakeGet({API: requests.ConnectTimeout("slow")}))
    with pytest.raises(gh.RepositoryDownloadError, match="timed out"):
        gh.download_and_extract_repo(URL, str(target))


def test_read_timeout_mid_download_is_reported_and_cleaned(monkeypatch, tmp_dirs):
    tmpdir, target = tmp_dirs
    resp = FakeResponse(200, chunks=[b"x" * 10, b"y" * 10, b"z" * 10], raise_mid=requests.ReadTimeout("stall"))
    monkeypatch.setattr(gh.requests, "get", FakeGet({API: resp}))
    with pytest.raises(gh.RepositoryDownloadError, match="timed out"):
        gh.download_and_extract_repo(URL, str(target))
    assert list(tmpdir.iterdir()) == [] and not target.exists() and resp.closed


def test_interrupted_download_is_reported(monkeypatch, tmp_dirs):
    tmpdir, target = tmp_dirs
    resp = FakeResponse(200, chunks=[b"a" * 10, b"b" * 10], raise_mid=requests.exceptions.ChunkedEncodingError("cut"))
    monkeypatch.setattr(gh.requests, "get", FakeGet({API: resp}))
    with pytest.raises(gh.RepositoryDownloadError, match="interrupted"):
        gh.download_and_extract_repo(URL, str(target))
    assert list(tmpdir.iterdir()) == []


def test_truncated_download_detected_via_content_length(monkeypatch, tmp_dirs):
    tmpdir, target = tmp_dirs
    body = _zip_bytes()
    resp = FakeResponse(200, body=body[:-20], headers={"Content-Length": str(len(body))})
    monkeypatch.setattr(gh.requests, "get", FakeGet({API: resp}))
    with pytest.raises(gh.RepositoryDownloadError, match="incomplete"):
        gh.download_and_extract_repo(URL, str(target))
    assert not target.exists() and list(tmpdir.iterdir()) == []


@pytest.mark.parametrize("status,match", [
    (404, "not found"),
    (403, "rate limited"),
    (429, "rate limited"),
    (500, "HTTP 500"),
    (503, "HTTP 503"),
])
def test_http_errors_are_translated(monkeypatch, tmp_dirs, status, match):
    _, target = tmp_dirs
    monkeypatch.setattr(gh.requests, "get", FakeGet({API: FakeResponse(status)}))
    with pytest.raises(gh.RepositoryDownloadError, match=match):
        gh.download_and_extract_repo(URL, str(target))
    assert not target.exists()


def test_invalid_zip_content_is_rejected_cleanly(monkeypatch, tmp_dirs):
    tmpdir, target = tmp_dirs
    body = b"<html>this is not a zip</html>"
    monkeypatch.setattr(gh.requests, "get", FakeGet({API: FakeResponse(200, body=body, headers={"Content-Length": str(len(body))})}))
    with pytest.raises(ValueError, match="not a valid ZIP"):
        gh.download_and_extract_repo(URL, str(target))
    assert list(tmpdir.iterdir()) == [] and not target.exists()


def test_malicious_archive_from_download_leaves_no_output(monkeypatch, tmp_dirs):
    tmpdir, target = tmp_dirs
    body = _zip_bytes({"repo/ok.py": "x=1\n", "../../escape.txt": "boom"})
    monkeypatch.setattr(gh.requests, "get", FakeGet({API: FakeResponse(200, body=body, headers={"Content-Length": str(len(body))})}))
    with pytest.raises(ValueError, match="Malicious archive"):
        gh.download_and_extract_repo(URL, str(target))
    assert not target.exists() and list(tmpdir.iterdir()) == []


def test_download_never_requests_urls_built_from_unvalidated_input(monkeypatch, tmp_dirs):
    _, target = tmp_dirs
    get = FakeGet({})
    monkeypatch.setattr(gh.requests, "get", get)
    with pytest.raises(ValueError):
        gh.download_and_extract_repo("https://github.com/psf/../../etc/passwd", str(target))
    with pytest.raises(ValueError):
        gh.download_and_extract_repo("https://evil.com/psf/requests", str(target))
    assert get.calls == []
