"""Provider selection, bounded retries, fallback, and grounding preserved through the fallback path."""
import pytest

import backend.llm as llm_mod
from backend.llm import (GeminiClient, LLMError, OpenRouterClient, ResilientLLM, TRANSIENT_STATUS,
                         build_llm_from_env, get_llm_client)
from backend.main import app
from tests.test_ask import Q, Resp, ask, env  # noqa: F401  (fixtures + helpers)

KEY = "k-secret-123456"


def _or_ok(text="hi [1]", finish="stop"):
    return Resp(200, {"choices": [{"message": {"content": text}, "finish_reason": finish}]})


class Scripted:
    """Fake LLMClient: pops outcomes (str -> reply, LLMError -> raised)."""

    def __init__(self, provider, model, outcomes):
        self.provider, self.model, self.outcomes, self.calls = provider, model, list(outcomes), 0

    def complete(self, system, user):
        self.calls += 1
        out = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(out, LLMError):
            raise out
        return out


T = lambda m="HTTP 503": LLMError(m, transient=True)  # noqa: E731
P = lambda m="HTTP 401": LLMError(m)  # noqa: E731


def resilient(*clients, **kw):
    sleeps = []
    kw.setdefault("rng", lambda: 0.5)  # no jitter
    r = ResilientLLM(list(clients), sleep=sleeps.append, **kw)
    r.sleeps = sleeps
    return r


# ------------------------------------------------------------------ OpenRouter adapter
def test_openrouter_request_shape_and_parsing(monkeypatch):
    seen = {}

    def post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, body=json, timeout=timeout)
        return _or_ok("answer [1]")

    monkeypatch.setattr(llm_mod.requests, "post", post)
    assert OpenRouterClient(api_key=KEY, model="google/gemini-2.5-flash", timeout=7).complete("SYS", "USER") == "answer [1]"
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions" and KEY not in seen["url"]
    assert seen["headers"]["Authorization"] == f"Bearer {KEY}" and seen["timeout"] == (10, 7)
    assert seen["body"]["temperature"] == 0 and seen["body"]["model"] == "google/gemini-2.5-flash"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "SYS"}


@pytest.mark.parametrize("resp,match,transient", [
    (Resp(429, {"error": {"message": "slow down"}}), "HTTP 429", True),
    (Resp(503, {}), "HTTP 503", True),
    (Resp(401, {"error": {"message": "invalid key"}}), "HTTP 401", False),
    (Resp(402, {}), "HTTP 402", False),
    (Resp(400, {}), "HTTP 400", False),
    (Resp(200, None), "non-JSON", False),
    (Resp(200, {"choices": []}), "no choices", False),
    (Resp(200, {"error": {"code": 503}}), "no choices", True),
    (_or_ok(finish="length"), "did not finish", False),
    (_or_ok(text=""), "empty", False),
])
def test_openrouter_failure_classification(monkeypatch, resp, match, transient):
    monkeypatch.setattr(llm_mod.requests, "post", lambda *a, **k: resp)
    with pytest.raises(LLMError, match=match) as e:
        OpenRouterClient(api_key=KEY).complete("s", "u")
    assert e.value.transient is transient


def test_openrouter_timeout_transient_and_missing_key(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ReadTimeout("slow")

    monkeypatch.setattr(llm_mod.requests, "post", boom)
    with pytest.raises(LLMError) as e:
        OpenRouterClient(api_key=KEY).complete("s", "u")
    assert e.value.transient
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    for key in ("", "YOUR_OPENROUTER_KEY", None):
        with pytest.raises(LLMError, match="OPENROUTER_API_KEY") as e:
            OpenRouterClient(api_key=key).complete("s", "u")
        assert not e.value.transient


@pytest.mark.parametrize("status,transient", [(429, True), (503, True), (500, False), (400, False),
                                              (401, False), (403, False), (404, False)])
def test_gemini_transient_classification(monkeypatch, status, transient):
    monkeypatch.setattr(llm_mod.requests, "post", lambda *a, **k: Resp(status, {"error": {"message": "x"}}))
    with pytest.raises(LLMError) as e:
        GeminiClient(api_key=KEY).complete("s", "u")
    assert e.value.transient is transient
    assert (status in TRANSIENT_STATUS) is transient


# ------------------------------------------------------------------ retries
def test_transient_failure_retries_with_exponential_backoff_then_succeeds():
    a = Scripted("gemini", "g", [T(), T(), "ok [1]"])
    r = resilient(a, max_retries=2, base_delay=1.0, max_delay=8.0)
    assert r.complete("s", "u") == "ok [1]" and a.calls == 3 and r.sleeps == [1.0, 2.0]


def test_retries_are_bounded_and_delay_capped():
    a = Scripted("gemini", "g", [T()])
    r = resilient(a, max_retries=4, base_delay=1.0, max_delay=3.0)
    with pytest.raises(LLMError, match="503"):
        r.complete("s", "u")
    assert a.calls == 5 and r.sleeps == [1.0, 2.0, 3.0, 3.0]   # 1 + max_retries attempts, capped delays


@pytest.mark.parametrize("err", [P("HTTP 401"), P("HTTP 400"), P("HTTP 403"), LLMError("LLM is not configured: x")])
def test_permanent_errors_are_never_retried(err):
    a = Scripted("gemini", "g", [err])
    r = resilient(a, max_retries=3)
    with pytest.raises(LLMError):
        r.complete("s", "u")
    assert a.calls == 1 and r.sleeps == []


def test_total_time_budget_stops_retrying():
    clock = iter([0.0, 100.0, 100.0, 100.0])  # started=0, first retry check sees t=100
    a = Scripted("gemini", "g", [T()])
    r = ResilientLLM([a], max_retries=5, base_delay=1.0, total_timeout=10.0, sleep=lambda s: None,
                     monotonic=lambda: next(clock), rng=lambda: 0.5)
    with pytest.raises(LLMError):
        r.complete("s", "u")
    assert a.calls == 1


# ------------------------------------------------------------------ fallback
def test_fallback_used_after_primary_retries_exhausted():
    a, b = Scripted("gemini", "g1", [T()]), Scripted("openrouter", "o1", ["fallback answer [1]"])
    r = resilient(a, b, max_retries=2)
    assert r.complete("s", "u") == "fallback answer [1]"
    assert a.calls == 3 and b.calls == 1 and r.model == "o1"


def test_fallback_used_immediately_for_invalid_primary_key_without_retry():
    a, b = Scripted("gemini", "g1", [P("HTTP 400. API key not valid")]), Scripted("openrouter", "o1", ["ok [1]"])
    r = resilient(a, b)
    assert r.complete("s", "u") == "ok [1]" and a.calls == 1 and r.sleeps == []


def test_fallback_clients_also_retry_transient_errors():
    a, b = Scripted("gemini", "g", [P()]), Scripted("openrouter", "o", [T(), "ok [1]"])
    r = resilient(a, b, max_retries=2)
    assert r.complete("s", "u") == "ok [1]" and b.calls == 2


def test_primary_success_never_touches_fallback():
    a, b = Scripted("gemini", "g1", ["ok [1]"]), Scripted("openrouter", "o1", ["x"])
    r = resilient(a, b)
    assert r.complete("s", "u") == "ok [1]" and b.calls == 0 and r.model == "g1"


def test_both_providers_failing_gives_one_controlled_error_without_secrets(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secret-key-987654")
    a = Scripted("gemini", "g1", [T("rate limit HTTP 429")])
    b = Scripted("openrouter", "o1", [P("HTTP 401 or-secret-key-987654")])
    with pytest.raises(LLMError) as e:
        resilient(a, b, max_retries=1).complete("s", "u")
    msg = str(e.value)
    assert "All configured LLM providers failed" in msg and "gemini/g1" in msg and "openrouter/o1" in msg
    assert a.calls == 2 and b.calls == 1


# ------------------------------------------------------------------ configuration from env
def _clear(monkeypatch):
    for k in ("LLM_PROVIDER", "LLM_MODEL", "LLM_FALLBACK_PROVIDER", "LLM_FALLBACK_MODEL", "LLM_MAX_RETRIES",
              "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_default_is_gemini_only_and_backward_compatible(monkeypatch):
    _clear(monkeypatch)
    r = build_llm_from_env()
    assert [(c.provider, c.model) for c in r.clients] == [("gemini", "gemini-2.5-flash")] and r.max_retries == 2
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        r.complete("s", "u")   # single client: original message preserved


def test_env_selects_providers_models_and_retries(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_MODEL", "gemini-2.5-pro")
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "OpenRouter")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "meta-llama/llama-3.3-70b-instruct")
    monkeypatch.setenv("LLM_MAX_RETRIES", "4")
    r = build_llm_from_env()
    assert [(c.provider, c.model) for c in r.clients] == [("gemini", "gemini-2.5-pro"),
                                                          ("openrouter", "meta-llama/llama-3.3-70b-instruct")]
    assert r.max_retries == 4
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "gemini")
    monkeypatch.delenv("LLM_FALLBACK_MODEL")
    assert [(c.provider, c.model) for c in build_llm_from_env().clients][1] == ("gemini", "gemini-2.5-flash")


def test_unknown_provider_is_a_controlled_error_not_a_crash(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "nope")
    with pytest.raises(LLMError, match="Unknown LLM provider 'nope'"):
        build_llm_from_env().complete("s", "u")


def test_missing_keys_on_both_providers(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "openrouter")
    with pytest.raises(LLMError) as e:
        build_llm_from_env().complete("s", "u")
    assert "GEMINI_API_KEY" in str(e.value) and "OPENROUTER_API_KEY" in str(e.value)


def test_end_to_end_http_mocked_gemini_503_then_openrouter(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key-123456")
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "openrouter")
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    urls = []

    def post(url, **kw):
        urls.append(url.split("/")[2])
        return Resp(503, {}) if "googleapis" in url else _or_ok("from openrouter [1]")

    monkeypatch.setattr(llm_mod.requests, "post", post)
    r = get_llm_client()
    assert r.complete("s", "u") == "from openrouter [1]"
    assert urls == ["generativelanguage.googleapis.com"] * 3 + ["openrouter.ai"] and r.model == "google/gemini-2.5-flash"


# ------------------------------------------------------------------ grounding through the fallback path
def _use(ask, *clients):
    llm = resilient(*clients, max_retries=1)
    app.dependency_overrides[get_llm_client] = lambda: llm
    return llm


def test_fallback_answer_is_grounded_and_cited_from_database(ask):
    a, b = Scripted("gemini", "g1", [T()]), Scripted("openrouter", "o1", ["It sums prices [1]."])
    _use(ask, a, b)
    body = ask().json()
    assert body["status"] == "answered" and body["model"] == "o1" and a.calls == 2
    c = body["citations"][0]
    assert c["chunk_id"] in [r[0] for r in ask.env.chunks(ask.repo_id)] and c["snippet"] and c["start_line"] >= 1


@pytest.mark.parametrize("reply", ["No citation here.", "Bogus [42].", "INSUFFICIENT_EVIDENCE"])
def test_fallback_answers_without_valid_citation_are_rejected(ask, reply):
    _use(ask, Scripted("gemini", "g1", [T()]), Scripted("openrouter", "o1", [reply]))
    body = ask().json()
    assert body["status"] == "insufficient_evidence" and body["answer"] is None and body["citations"] == []


def test_evidence_gate_blocks_both_providers(ask, monkeypatch):
    monkeypatch.setenv("ASK_MIN_SCORE", "0.99")
    a, b = Scripted("gemini", "g1", ["x [1]"]), Scripted("openrouter", "o1", ["x [1]"])
    _use(ask, a, b)
    assert ask("zebra giraffe unrelated").json()["status"] == "insufficient_evidence"
    assert a.calls == 0 and b.calls == 0


def test_both_failing_returns_sanitized_503(ask, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-secret-key-123456")
    _use(ask, Scripted("gemini", "g1", [T("HTTP 503 AIza-secret-key-123456")]), Scripted("openrouter", "o1", [P("HTTP 401")]))
    res = ask()
    assert res.status_code == 503 and "LLM unavailable" in res.json()["detail"]
    assert "AIza-secret" not in res.text and "gemini/g1" in res.json()["detail"]
