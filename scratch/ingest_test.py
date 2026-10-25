"""Manual end-to-end check: submit a repository, wait for ITS job, then search.

Usage (PowerShell, backend already running on port 8000):

    python scratch/ingest_test.py
    python scratch/ingest_test.py --url https://github.com/psf/requests --timeout 600 --interval 3

Exit codes:
    0  ingestion job succeeded (and the search check passed)
    1  the ingestion job FAILED (its error log is printed)
    2  timed out while the job was still pending/running
    3  could not submit / poll (backend unreachable or unexpected HTTP response)
    4  ingestion succeeded but the search check failed
"""
import argparse
import sys
import time

import requests

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_REPO = "https://github.com/psf/requests"
DEFAULT_INTERVAL = 2.0   # seconds between polls
DEFAULT_TIMEOUT = 300.0  # seconds to wait for the job (first run downloads the embedding model)

EXIT_OK, EXIT_JOB_FAILED, EXIT_TIMEOUT, EXIT_REQUEST_ERROR, EXIT_SEARCH_FAILED = 0, 1, 2, 3, 4


def ingest(base_url, github_url, http=requests):
    """Submit the repository. Returns (repository_id, job_id) of THIS request."""
    resp = http.post(f"{base_url}/repositories/", json={"github_url": github_url}, timeout=30)
    print("Ingest response:", resp.status_code, resp.text)
    if resp.status_code != 200:
        raise RuntimeError(f"Ingest request failed with HTTP {resp.status_code}")
    data = resp.json()
    return data["repository_id"], data["job_id"]


def wait_job(base_url, repo_id, job_id, interval=DEFAULT_INTERVAL, timeout=DEFAULT_TIMEOUT,
             http=requests, sleep=time.sleep, monotonic=time.monotonic):
    """Poll until the job with exactly ``job_id`` finishes.

    Returns (outcome, job) where outcome is "succeeded", "failed" or "timeout".
    Other (older) jobs of the same repository are ignored.
    """
    deadline = monotonic() + timeout
    job = None
    while True:
        resp = http.get(f"{base_url}/repositories/{repo_id}/jobs", timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Job status request failed with HTTP {resp.status_code}")
        job = next((j for j in resp.json() if j.get("id") == job_id), None)
        if job is None:
            raise RuntimeError(f"Job #{job_id} not found for repository {repo_id}")
        status = job.get("status")
        if status == "succeeded":
            return "succeeded", job
        if status == "failed":
            return "failed", job
        if monotonic() >= deadline:
            return "timeout", job
        sleep(interval)


def search(base_url, repo_id, query="session", top_k=5, http=requests):
    resp = http.post(f"{base_url}/repositories/{repo_id}/search",
                     json={"query": query, "top_k": top_k}, timeout=60)
    print("Search response:", resp.status_code, resp.text[:2000])
    return resp.status_code == 200 and isinstance(resp.json(), list) and len(resp.json()) > 0


def run(base_url=DEFAULT_BASE_URL, github_url=DEFAULT_REPO, interval=DEFAULT_INTERVAL,
        timeout=DEFAULT_TIMEOUT, query="session", http=requests, sleep=time.sleep,
        monotonic=time.monotonic) -> int:
    try:
        repo_id, job_id = ingest(base_url, github_url, http)
    except (RuntimeError, requests.RequestException) as exc:
        print(f"Could not submit repository: {exc}")
        return EXIT_REQUEST_ERROR
    print(f"Repo ID: {repo_id}, Job ID: {job_id} (polling every {interval}s, timeout {timeout}s)")

    try:
        outcome, job = wait_job(base_url, repo_id, job_id, interval, timeout, http, sleep, monotonic)
    except (RuntimeError, requests.RequestException) as exc:
        print(f"Could not read job status: {exc}")
        return EXIT_REQUEST_ERROR

    if outcome == "failed":
        print(f"Ingestion job #{job_id} FAILED: {job.get('log')}")
        return EXIT_JOB_FAILED
    if outcome == "timeout":
        print(f"TIMEOUT: job #{job_id} was still '{job.get('status')}' after {timeout}s. "
              "It may still finish; check GET /repositories/{id}/jobs.")
        return EXIT_TIMEOUT

    print(f"Ingestion job #{job_id} succeeded: {job.get('log')}")
    if search(base_url, repo_id, query, http=http):
        print("Search check passed.")
        return EXIT_OK
    print("Search check FAILED (non-200 response or no results).")
    return EXIT_SEARCH_FAILED


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--url", default=DEFAULT_REPO, help="GitHub repository URL to ingest")
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="seconds between polls")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="max seconds to wait for the job")
    p.add_argument("--query", default="session", help="search query used after a successful ingest")
    args = p.parse_args(argv)
    return run(args.base_url, args.url, args.interval, args.timeout, args.query)


if __name__ == "__main__":
    sys.exit(main())
