import os
import zipfile
import io
import stat
import requests
from urllib.parse import urlparse

# Max allowed uncompressed size: 500 MB
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
# Max allowed file count to prevent inode exhaustion / decompression bombs
MAX_FILE_COUNT = 50000


def parse_github_url(github_url: str):
    """Extract owner and repo name from GitHub URL."""
    parsed = urlparse(github_url)
    if not parsed.netloc or "github.com" not in parsed.netloc.lower():
        raise ValueError(f"URL must be a valid GitHub URL: {github_url}")

    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(path_parts) < 2:
        raise ValueError(f"Invalid GitHub repository URL: {github_url}")

    owner, repo = path_parts[0], path_parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo


def is_symlink_member(member: zipfile.ZipInfo) -> bool:
    """Checks whether a ZipInfo entry is marked as a symbolic link (Unix mode)."""
    # High 16 bits of external_attr in Unix archives contain file mode
    mode = member.external_attr >> 16
    return stat.S_ISLNK(mode)



def safe_extract_zip(zip_file: zipfile.ZipFile, target_dir: str):
    """Safely extract regular files and directories from a ZIP archive.

    Rejects path traversal, absolute paths, oversized archives, and
    excessive file counts. Symbolic links are skipped.
    """


    total_uncompressed_size = 0

    infolist = zip_file.infolist()
    if len(infolist) > MAX_FILE_COUNT:
        raise ValueError(
            f"Archive contains too many entries "
            f"({len(infolist)} > {MAX_FILE_COUNT})"
        )

    safe_members = []

    for member in infolist:
        total_uncompressed_size += member.file_size
        if total_uncompressed_size > MAX_UNCOMPRESSED_BYTES:
            raise ValueError(
                f"Archive uncompressed size exceeds limit of "
                f"{MAX_UNCOMPRESSED_BYTES / (1024 * 1024):.0f} MB"
            )

        # Normalize the path before validating it.
        norm_name = member.filename.replace("\\", "/")

        if (
            norm_name.startswith("/")
            or (len(norm_name) > 1 and norm_name[1] == ":")
        ):
            raise ValueError(
                f"Malicious archive detected: '{member.filename}' "
                "contains an absolute path."
            )

        dest_path = os.path.abspath(
            os.path.join(abs_target_dir, norm_name)
        )

        if not (
            dest_path == abs_target_dir
            or dest_path.startswith(abs_target_dir + os.sep)
        ):
            raise ValueError(
                f"Malicious archive detected: '{member.filename}' "
                "escapes target extraction directory."
            )

        # Skip symbolic links instead of rejecting the whole archive.
        if is_symlink_member(member):
            continue

        safe_members.append(member)

    # Extract only entries that passed validation.
    for member in safe_members:
        zip_file.extract(member, abs_target_dir)
    """Safely extracts a ZipFile preventing:
    1. Zip Slip (path traversal / absolute paths / Windows drive prefixes)
    2. Decompression bombs (excessive bytes or file count)
    3. Symlink / hardlink security risks (arbitrary file overwrite)
    """




def download_and_extract_repo(github_url: str, target_dir: str) -> str:
    """Download the default zipball for a repository and extract it safely into target_dir.

    Returns the absolute path to the directory where contents were extracted.
    """
    owner, repo = parse_github_url(github_url)
    zip_url = f"https://api.github.com/repos/{owner}/{repo}/zipball"

    headers = {"User-Agent": "RepoPilot-Backend"}
    # requests automatically handles redirects (up to 30) for stream=True
    response = requests.get(zip_url, headers=headers, stream=True, timeout=60, allow_redirects=True)
    response.raise_for_status()

    os.makedirs(target_dir, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        safe_extract_zip(z, target_dir)

    # zipball extracts inside a single root folder (owner-repo-sha)
    extracted_items = os.listdir(target_dir)
    if extracted_items:
        first_item = os.path.join(target_dir, extracted_items[0])
        if os.path.isdir(first_item):
            return first_item

    return target_dir
