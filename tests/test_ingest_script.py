"""Tests for scratch/ingest_test.py: success, failure and timeout must be distinguishable."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scratch" / "ingest_test.py"
spec = importlib.util.spec_from_file_location("scratch_ingest_script", SCRIPT)
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeHttp:
    """Scripted HTTP: `job_states` is the sequence of statuses returned for the current job."""

    def __init__(self, job_states, job_id=7, older_jobs=(), search_payload=None, search_status=200,
                 ingest_status=200, fail_log=None):
        self.job_states = list(job_states)
        self.job_id = job_id
        self.older_jobs = list(older_jobs)
        self.search_payload = search_payload if search_payload is not None else [{"id": 1, "score": 0.9}]
        self.search_status = search_status
        self.ingest_status = ingest_status
        self.fail_log = fail_log
        self.polls = 0

    def post(self, url, json=None, timeout=None):
        if url.endswith("/repositories/"):
            if self.ingest_status != 200:
                return FakeResponse(self.ingest_status, {"detail": "bad"})
            return FakeResponse(200, {"repository_id": 3, "job_id": self.job_id, "message": "ok"})
        return FakeResponse(self.search_status, self.search_payload)

    def get(self, url, timeout=None):
        state = self.job_states[min(self.polls, len(self.job_states) - 1)]
        self.polls += 1
        current = {"id": self.job_id, "status": state, "log": self.fail_log if state == "failed" else "ok"}
        return FakeResponse(200, [current] + self.older_jobs)


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def monotonic(self):
        return self.t


def _run(http, timeout=20, interval=2):
    clock = Clock()
    code = script.run("http://x", "https://github.com/a/b", interval, timeout, "q", http,
                      clock.sleep, clock.monotonic)
    return code, clock


def test_success_exit_code_zero_after_polling():
    code, clock = _run(FakeHttp(["pending", "running", "succeeded"]))
    assert code == script.EXIT_OK == 0
    assert clock.sleeps == [2, 2]


def test_failed_job_stops_immediately_and_prints_real_error_not_timeout(capsys):
    http = FakeHttp(["running", "failed"], fail_log="Ingestion failed during parsing: boom")
    code, clock = _run(http, timeout=1000)
    out = capsys.readouterr().out
    assert code == script.EXIT_JOB_FAILED == 1
    assert "FAILED" in out and "Ingestion failed during parsing: boom" in out
    assert "TIMEOUT" not in out
    assert http.polls == 2          # stopped at the first 'failed', did not keep polling
    assert len(clock.sleeps) == 1


def test_timeout_exit_code_and_message(capsys):
    code, clock = _run(FakeHttp(["running"]), timeout=6, interval=2)
    out = capsys.readouterr().out
    assert code == script.EXIT_TIMEOUT == 2
    assert "TIMEOUT" in out and "running" in out


def test_older_successful_job_is_ignored():
    older = [{"id": 1, "status": "succeeded", "log": "old"}]
    code, _ = _run(FakeHttp(["running"], job_id=2, older_jobs=older), timeout=6)
    assert code == script.EXIT_TIMEOUT  # the old success must not count


def test_older_failed_job_does_not_fail_the_current_job():
    older = [{"id": 1, "status": "failed", "log": "old failure"}]
    code, _ = _run(FakeHttp(["running", "succeeded"], job_id=2, older_jobs=older))
    assert code == script.EXIT_OK


def test_search_failure_after_successful_ingest_is_nonzero():
    code, _ = _run(FakeHttp(["succeeded"], search_status=503, search_payload={"detail": "down"}))
    assert code == script.EXIT_SEARCH_FAILED == 4


def test_empty_search_results_after_ingest_is_nonzero():
    code, _ = _run(FakeHttp(["succeeded"], search_payload=[]))
    assert code == script.EXIT_SEARCH_FAILED


def test_ingest_http_error_is_request_error():
    code, _ = _run(FakeHttp(["succeeded"], ingest_status=400))
    assert code == script.EXIT_REQUEST_ERROR == 3


def test_unreachable_backend_is_request_error():
    import requests

    class Down:
        def post(self, *a, **k):
            raise requests.ConnectionError("refused")

    code, _ = _run(Down())
    assert code == script.EXIT_REQUEST_ERROR


def test_missing_job_in_listing_is_request_error():
    class Gone(FakeHttp):
        def get(self, url, timeout=None):
            return FakeResponse(200, [{"id": 999, "status": "succeeded", "log": ""}])

    code, _ = _run(Gone(["running"]))
    assert code == script.EXIT_REQUEST_ERROR
