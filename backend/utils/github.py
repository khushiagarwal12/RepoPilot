import os
import re
import shutil
import stat
import tempfile
import time
import zipfile
from urllib.parse import urljoin, urlparse

import requests

# Max allowed uncompressed size: 500 MB
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
# Max allowed file count to prevent inode exhaustion / decompression bombs
MAX_FILE_COUNT = 50000
# Max size of the downloaded (compressed) archive: 200 MB
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
# (connect, read) timeouts per network call, and a hard cap on the whole download
CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 30
MAX_DOWNLOAD_SECONDS = 300
MAX_REDIRECTS = 3

# Only these hosts are ever contacted when downloading (the zipball API redirects
# to codeload.github.com). Redirects to anything else are refused (SSRF guard).
ALLOWED_DOWNLOAD_HOSTS = frozenset({"api.github.com", "codeload.github.com"})
ALLOWED_REPO_HOSTS = frozenset({"github.com", "www.github.com"})

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class RepositoryDownloadError(RuntimeError):
    """Raised when a repository archive cannot be downloaded (network/HTTP/size problems)."""


def parse_github_url(github_url: str):
    """Validate a GitHub repository URL and return ``(owner, repo)``.

    Accepted form: ``https://github.com/<owner>/<repo>`` (optionally with a
    ``.git`` suffix, a trailing slash, or ``www.``). Everything else is rejected:
    other hosts or look-alike domains (``github.com.evil.com``), non-HTTPS
    schemes, embedded credentials, custom ports, extra path segments, and
    owner/repo names with characters GitHub does not allow.
    """
    if not isinstance(github_url, str) or not github_url.strip() or github_url != github_url.strip():
        raise ValueError(f"URL must be a valid GitHub URL: {github_url!r}")
    try:
        parsed = urlparse(github_url)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        raise ValueError(f"URL must be a valid GitHub URL: {github_url}")

    if parsed.scheme != "https":
        raise ValueError(f"URL must use https: {github_url}")
    if hostname not in ALLOWED_REPO_HOSTS:
        raise ValueError(f"URL must be a valid GitHub URL: {github_url}")
    if parsed.username or parsed.password or port not in (None, 443):
        raise ValueError(f"URL must not contain credentials or a custom port: {github_url}")

    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) != 2:
        raise ValueError(
            f"Invalid GitHub repository URL (expected https://github.com/<owner>/<repo>): {github_url}"
        )

    owner, repo = path_parts
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not _OWNER_RE.match(owner) or not _REPO_RE.match(repo) or repo in (".", ".."):
        raise ValueError(f"Invalid GitHub owner or repository name: {github_url}")
    return owner, repo


def _unix_mode(member: zipfile.ZipInfo) -> int:
    """Return the Unix file mode stored in a ZIP entry, or 0 if there is none.

    The high 16 bits of ``external_attr`` only hold a Unix mode when the
    archive was created on Unix (``create_system == 3``).
    """
    if member.create_system != 3:
        return 0
    return member.external_attr >> 16


def is_symlink_member(member: zipfile.ZipInfo) -> bool:
    """Checks whether a ZipInfo entry is marked as a symbolic link (Unix mode)."""
    return stat.S_ISLNK(_unix_mode(member))


def _is_unsafe_special_member(member: zipfile.ZipInfo) -> bool:
    """True for device nodes, FIFOs and sockets, which must never be extracted."""
    mode = _unix_mode(member)
    return (
        stat.S_ISCHR(mode)
        or stat.S_ISBLK(mode)
        or stat.S_ISFIFO(mode)
        or stat.S_ISSOCK(mode)
    )


_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_COPY_CHUNK = 64 * 1024


def _validated_destination(member: zipfile.ZipInfo, abs_target_dir: str) -> str:
    """Validate a member name and return its absolute destination path."""
    name = member.filename.replace("\\", "/")

    if "\x00" in name:
        raise ValueError(
            f"Malicious archive detected: '{member.filename!r}' contains a NUL byte."
        )
    if name.startswith("/") or _DRIVE_PREFIX.match(name):
        raise ValueError(
            f"Malicious archive detected: '{member.filename}' contains an absolute path."
        )
    if ".." in name.split("/"):
        raise ValueError(
            f"Malicious archive detected: '{member.filename}' escapes target extraction directory."
        )

    dest_path = os.path.abspath(os.path.join(abs_target_dir, name))
    if not (
        dest_path == abs_target_dir
        or dest_path.startswith(abs_target_dir + os.sep)
    ):
        raise ValueError(
            f"Malicious archive detected: '{member.filename}' escapes target extraction directory."
        )
    return dest_path


def safe_extract_zip(zip_file: zipfile.ZipFile, target_dir: str) -> dict:
    """Safely extract regular files and directories from a ZIP archive.

    Guarantees:
    - Every member is validated BEFORE any byte is written: absolute paths,
      Windows drive paths, ``..`` traversal, destinations outside ``target_dir``,
      device/FIFO/socket entries, entry count and declared total size.
    - Symbolic-link entries are skipped (legitimate GitHub archives contain
      them); they are never created on disk, so nothing can be written
      through a link.
    - Actual bytes written are counted while streaming, so lying ZIP headers
      cannot bypass the size limit.
    - Extraction happens in a staging directory next to ``target_dir`` and is
      moved into place only if everything succeeded. A rejected or failed
      archive leaves ``target_dir`` untouched and no staging output behind.

    ``target_dir`` must not exist, or must be an empty directory.

    Returns ``{"files": int, "skipped_symlinks": [names...]}``.
    """
    abs_target_dir = os.path.abspath(target_dir)

    if os.path.isdir(abs_target_dir) and os.listdir(abs_target_dir):
        raise ValueError(f"Extraction target is not empty: {target_dir}")

    infolist = zip_file.infolist()
    if len(infolist) > MAX_FILE_COUNT:
        raise ValueError(
            f"Archive contains too many entries "
            f"({len(infolist)} > {MAX_FILE_COUNT})"
        )

    # ---- Pass 1: validate everything before writing anything -------------
    declared_total = 0
    plan = []  # (member, relative destination path, is_dir)
    skipped_symlinks = []

    for member in infolist:
        declared_total += member.file_size
        if declared_total > MAX_UNCOMPRESSED_BYTES:
            raise ValueError(
                f"Archive uncompressed size exceeds limit of "
                f"{MAX_UNCOMPRESSED_BYTES / (1024 * 1024):.0f} MB"
            )

        dest_path = _validated_destination(member, abs_target_dir)

        if is_symlink_member(member):
            skipped_symlinks.append(member.filename)
            continue
        if _is_unsafe_special_member(member):
            raise ValueError(
                f"Malicious archive detected: '{member.filename}' is an unsafe special file."
            )

        rel = os.path.relpath(dest_path, abs_target_dir)
        plan.append((member, rel, member.is_dir()))

    # ---- Pass 2: stream into a staging dir with real byte limits ---------
    parent = os.path.dirname(abs_target_dir)
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=os.path.basename(abs_target_dir) + ".extract-", dir=parent)

    actual_total = 0
    files_written = 0
    try:
        for member, rel, is_dir in plan:
            out_path = os.path.join(staging, rel)
            if is_dir:
                os.makedirs(out_path, exist_ok=True)
                continue

            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            member_bytes = 0
            with zip_file.open(member) as src, open(out_path, "wb") as dst:
                while True:
                    chunk = src.read(_COPY_CHUNK)
                    if not chunk:
                        break
                    member_bytes += len(chunk)
                    actual_total += len(chunk)
                    if member_bytes > member.file_size:
                        raise ValueError(
                            f"Malicious archive detected: '{member.filename}' "
                            "is larger than its declared size."
                        )
                    if actual_total > MAX_UNCOMPRESSED_BYTES:
                        raise ValueError(
                            f"Archive uncompressed size exceeds limit of "
                            f"{MAX_UNCOMPRESSED_BYTES / (1024 * 1024):.0f} MB"
                        )
                    dst.write(chunk)
            files_written += 1

        # Success: move the fully-validated tree into place.
        if os.path.isdir(abs_target_dir):
            os.rmdir(abs_target_dir)  # known to be empty (checked above)
        os.replace(staging, abs_target_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return {"files": files_written, "skipped_symlinks": skipped_symlinks}


def _check_download_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_DOWNLOAD_HOSTS:
        raise RepositoryDownloadError(
            f"Refusing to download from unexpected location: {parsed.scheme}://{parsed.hostname}"
        )


def _open_archive_response(zip_url: str):
    """GET the zipball, following at most MAX_REDIRECTS redirects to allow-listed hosts only."""
    headers = {"User-Agent": "RepoPilot-Backend", "Accept": "application/vnd.github+json"}
    url = zip_url
    for _ in range(MAX_REDIRECTS + 1):
        _check_download_url(url)
        try:
            resp = requests.get(
                url,
                headers=headers,
                stream=True,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                allow_redirects=False,
            )
        except requests.Timeout as exc:
            raise RepositoryDownloadError("Download timed out while contacting GitHub.") from exc
        except requests.RequestException as exc:
            raise RepositoryDownloadError(f"Could not reach GitHub: {type(exc).__name__}") from exc

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise RepositoryDownloadError("GitHub redirect had no Location header.")
            url = urljoin(url, location)
            continue
        return resp
    raise RepositoryDownloadError("Too many redirects while downloading the repository.")


def _raise_for_status(resp) -> None:
    code = resp.status_code
    if code == 200:
        return
    if code == 404:
        raise RepositoryDownloadError("Repository not found on GitHub (or it is private).")
    if code in (403, 429):
        raise RepositoryDownloadError(
            f"GitHub refused the request (HTTP {code}); you may be rate limited. Try again later."
        )
    raise RepositoryDownloadError(f"GitHub returned HTTP {code} while downloading the repository.")


def _download_to_file(resp, dest_path: str) -> int:
    """Stream the response body to ``dest_path`` enforcing size and total-time limits."""
    declared = resp.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
        raise RepositoryDownloadError(
            f"Repository archive is too large ({int(declared)} bytes > {MAX_DOWNLOAD_BYTES})."
        )

    deadline = time.monotonic() + MAX_DOWNLOAD_SECONDS
    received = 0
    try:
        with open(dest_path, "wb") as out:
            for chunk in resp.iter_content(chunk_size=_COPY_CHUNK):
                if not chunk:
                    continue
                received += len(chunk)
                if received > MAX_DOWNLOAD_BYTES:
                    raise RepositoryDownloadError(
                        f"Repository archive is too large (more than {MAX_DOWNLOAD_BYTES} bytes)."
                    )
                if time.monotonic() > deadline:
                    raise RepositoryDownloadError(
                        f"Download exceeded the {MAX_DOWNLOAD_SECONDS}s time limit."
                    )
                out.write(chunk)
    except requests.Timeout as exc:
        raise RepositoryDownloadError("Download timed out while reading the archive.") from exc
    except requests.RequestException as exc:
        raise RepositoryDownloadError(f"Download was interrupted: {type(exc).__name__}") from exc

    if declared and declared.isdigit() and received != int(declared):
        raise RepositoryDownloadError(
            f"Download was incomplete ({received} of {declared} bytes)."
        )
    return received


def download_and_extract_repo(github_url: str, target_dir: str) -> str:
    """Download the default-branch zipball of a public repository and extract it safely.

    The archive is streamed to a temporary file (never fully held in memory) with
    a size cap and a time cap, validated as a ZIP, then extracted with
    :func:`safe_extract_zip`. The temporary file is always removed and a failed
    run leaves nothing behind in ``target_dir``.

    Returns the absolute path to the directory where contents were extracted.
    """
    owner, repo = parse_github_url(github_url)
    zip_url = f"https://api.github.com/repos/{owner}/{repo}/zipball"

    fd, tmp_zip = tempfile.mkstemp(prefix="repopilot_", suffix=".zip")
    os.close(fd)
    created_target = not os.path.exists(target_dir)
    try:
        resp = _open_archive_response(zip_url)
        try:
            _raise_for_status(resp)
            _download_to_file(resp, tmp_zip)
        finally:
            resp.close()

        try:
            zf = zipfile.ZipFile(tmp_zip)
        except zipfile.BadZipFile as exc:
            raise ValueError("Downloaded file is not a valid ZIP archive.") from exc
        with zf:
            safe_extract_zip(zf, target_dir)
    except BaseException:
        if created_target:
            shutil.rmtree(target_dir, ignore_errors=True)
        raise
    finally:
        try:
            os.unlink(tmp_zip)
        except OSError:
            pass

    # zipball extracts inside a single root folder (owner-repo-sha)
    extracted_items = os.listdir(target_dir)
    if extracted_items:
        first_item = os.path.join(target_dir, extracted_items[0])
        if os.path.isdir(first_item):
            return first_item

    return target_dir
