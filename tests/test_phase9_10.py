"""Retrieval improvements (dedupe, hybrid), CORS, repo list, logging, rate limiting, safe errors."""
import json
import logging
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

import backend.ratelimit as ratelimit
from backend.ask import answer_question
from backend.logging_config import JsonFormatter
from backend.main import app
from backend.retrieval import Retrieval, dedupe_overlaps, fuse_rrf, query_tokens
from backend.utils.errors import redact_text
from tests.test_vector_pipeline import ALPHA_V1, URL_A, env  # noqa: F401


def C(i, f, a, b):
    return NS(id=i, file_path=f, start_line=a, end_line=b, content="", symbol_name=None)


# ------------------------------------------------------------------ retrieval
def test_dedupe_drops_overlapping_chunks_same_file_only():
    hits = [(C(1, "a.py", 1, 100), .9), (C(2, "a.py", 80, 180), .8), (C(3, "a.py", 101, 150), .7),
            (C(4, "b.py", 1, 100), .6)]
    kept = [c.id for c, _ in dedupe_overlaps(hits)]

    assert 1 in kept and 4 in kept            # other file never deduped
    # chunk 2 overlaps 21 of its 101 lines (<50%) so it stays; identical ranges are dropped
    assert [c.id for c, _ in dedupe_overlaps([(C(1, "a.py", 1, 50), .9), (C(2, "a.py", 1, 50), .8)])] == [1]


def test_fuse_rrf_promotes_items_found_by_both_and_keeps_vector_score():
    v = [(C(1, "a", 1, 2), .9), (C(2, "a", 3, 4), .8)]
    lex = [(C(2, "a", 3, 4), 5.0), (C(3, "a", 5, 6), 4.0)]
    fused = fuse_rrf(v, lex)
    assert [c.id for c, _ in fused] == [2, 1, 3]
    assert dict((c.id, s) for c, s in fused) == {2: .8, 1: .9, 3: 0.0}


def test_query_tokens_drops_stopwords_and_short_words():
    assert query_tokens("How does the send_email_notification work?") == ["send_email_notification", "work"]


def test_hybrid_finds_exact_identifier_and_mode_is_overridable(env):  # noqa: F811
    env.set_source(URL_A, ALPHA_V1)
    repo_id, _ = env.ingest(URL_A)
    r = env.client.post(f"/repositories/{repo_id}/search", json={"query": "send_email_notification", "top_k": 3, "mode": "hybrid"})
    assert r.status_code == 200 and r.json()[0]["symbol_name"] == "send_email_notification"
    assert env.client.post(f"/repositories/{repo_id}/search", json={"query": "x", "mode": "bogus"}).status_code == 422


def test_ask_gate_uses_best_score_not_first_hit():
    class L:
        model = "m"
        calls = 0

        def complete(self, s, u):
            L.calls += 1
            return "ok [1]"

    chunk = lambda i: NS(id=i, file_path="a.py", start_line=1, end_line=2, content="x")
    assert answer_question("q", Retrieval(1, [(chunk(1), 0.0), (chunk(2), 0.9)]), L(), 0.5).status == "answered"
    assert answer_question("q", Retrieval(1, [(chunk(1), 0.1)]), L(), 0.5).status == "insufficient_evidence"


# ------------------------------------------------------------------ API surface
def test_repository_list_reports_real_status(env):  # noqa: F811
    assert env.client.get("/repositories/").json() == []
    env.set_source(URL_A, ALPHA_V1)
    repo_id, job_id = env.ingest(URL_A)
    item = env.client.get("/repositories/").json()[0]
    assert item["id"] == repo_id and item["indexed"] is True and item["latest_job"] == {"id": job_id, "status": "succeeded"}


def test_cors_allows_dev_frontend_only():
    c = TestClient(app)
    ok = c.options("/repositories/", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    bad = c.options("/repositories/", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in bad.headers


def test_request_id_header_and_safe_500():
    async def boom():
        raise RuntimeError("secret internals sk-abcdefghijklmnopqrstuvwxyz")

    app.add_api_route("/__boom", boom)
    try:
        r = TestClient(app, raise_server_exceptions=False).get("/__boom")
    finally:
        app.router.routes.pop()
    assert r.status_code == 500 and r.json()["detail"] == "Internal server error"
    assert "secret" not in r.text and "Traceback" not in r.text
    assert r.json()["request_id"] == r.headers["X-Request-ID"]
    assert TestClient(app).get("/health").headers["X-Request-ID"]


# ------------------------------------------------------------------ logging / secrets / limits
def test_log_lines_are_json_and_redact_secrets(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "my-very-secret-value")
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1,
                            "failed with my-very-secret-value and postgresql://u:hunter2@h/db and sk-abcdefghijklmnopqrstuvwxyz", None, None)
    line = JsonFormatter().format(rec)
    data = json.loads(line)
    assert data["level"] == "ERROR" and "request_id" in data
    for leak in ("my-very-secret-value", "hunter2", "sk-abcdefghijklmnopqrstuvwxyz"):
        assert leak not in line


def test_redact_text_masks_google_and_github_keys():
    assert redact_text("k=AIza" + "A" * 35 + " t=ghp_" + "b" * 25) == "k=*** t=***"


def test_rate_limit_returns_429_with_retry_after(env, monkeypatch):  # noqa: F811
    env.set_source(URL_A, ALPHA_V1)
    repo_id, _ = env.ingest(URL_A)
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "2")
    ratelimit.reset()
    codes = [env.client.post(f"/repositories/{repo_id}/search", json={"query": "email"}).status_code for _ in range(3)]
    r = env.client.post(f"/repositories/{repo_id}/search", json={"query": "email"})
    ratelimit.reset()
    assert codes == [200, 200, 429] and r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


def test_eval_script_metrics():
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("ev", pathlib.Path(__file__).parent.parent / "scratch" / "eval_retrieval.py")
    ev = importlib.util.module_from_spec(spec); spec.loader.exec_module(ev)
    assert ev.score([[False, True], [True], [False, False]]) == (2 / 3, (0.5 + 1 + 0) / 3)
    assert ev.relevant({"file_path": "src/requests/auth.py", "symbol_name": None}, ["AUTH.py"])
