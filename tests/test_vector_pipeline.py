"""Integration tests for chunk persistence, vector indexing, search and re-ingestion.

These use REAL ChromaDB (in a temp directory) and a real SQLite session; only the
neural embedding model is replaced by a deterministic stub (see conftest.py) and
the GitHub download is replaced by a local directory.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.embeddings as embeddings
import backend.routers.repo as repo_router
import backend.vector_store as vector_store
from backend.database import Base, get_db
from backend.main import app
from backend.models import CodeChunk, IngestionJob
from backend.utils.errors import sanitize_error

ALPHA_V1 = (
    "def compute_invoice_total(items):\n"
    "    return sum(item.price for item in items)\n\n"
    "def send_email_notification(address):\n"
    "    return address\n"
)
ALPHA_V2 = "def render_dashboard_widget(theme):\n    return theme\n"
BETA_V1 = (
    "class WeatherForecastClient:\n"
    "    def fetch_temperature(self, city):\n"
    "        return city\n"
)


class Env:
    def __init__(self, client, session_factory, tmp_path, monkeypatch):
        self.client = client
        self.Session = session_factory
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.sources = {}

    def set_source(self, url, code, filename="mod.py"):
        d = self.tmp_path / ("src_" + url.rsplit("/", 1)[-1])
        d.mkdir(exist_ok=True)
        (d / filename).write_text(code, encoding="utf-8")
        self.sources[url] = str(d)

    def ingest(self, url):
        res = self.client.post("/repositories/", json={"github_url": url})
        assert res.status_code == 200, res.text
        data = res.json()
        return data["repository_id"], data["job_id"]

    def job(self, job_id):
        db = self.Session()
        try:
            j = db.query(IngestionJob).filter(IngestionJob.id == job_id).first()
            return j.status, j.log
        finally:
            db.close()

    def chunks(self, repo_id):
        db = self.Session()
        try:
            return [(c.id, c.symbol_name, c.content) for c in
                    db.query(CodeChunk).filter(CodeChunk.repository_id == repo_id).all()]
        finally:
            db.close()

    def collections(self):
        return sorted(c.name for c in vector_store._get_client().list_collections())

    def search(self, repo_id, query, top_k=5):
        return self.client.post(f"/repositories/{repo_id}/search", json={"query": query, "top_k": top_k})


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    e = Env(TestClient(app), Session, tmp_path, monkeypatch)

    monkeypatch.setattr(repo_router, "download_and_extract_repo", lambda url, target: e.sources[url])
    orig = repo_router.process_repository_ingestion
    monkeypatch.setattr(
        repo_router, "process_repository_ingestion",
        lambda repo_id, job_id, url, **kw: orig(repo_id, job_id, url, db_factory=Session),
    )
    yield e
    app.dependency_overrides.clear()
    engine.dispose()


URL_A = "https://github.com/example/alpha"
URL_B = "https://github.com/example/beta"


# ---------------------------------------------------------------- persistence / indexing

def test_persisted_chunk_ids_are_non_null_and_unique(env):
    env.set_source(URL_A, ALPHA_V1)
    repo_id, job_id = env.ingest(URL_A)
    assert env.job(job_id)[0] == "succeeded"

    ids = [c[0] for c in env.chunks(repo_id)]
    assert len(ids) >= 2
    assert all(i is not None for i in ids)
    assert len(set(ids)) == len(ids)


def test_chunks_are_indexed_in_real_chroma_matching_database(env):
    env.set_source(URL_A, ALPHA_V1)
    repo_id, job_id = env.ingest(URL_A)

    coll = vector_store._get_client().get_collection(vector_store.collection_name(repo_id, job_id))
    stored = coll.get(include=["metadatas"])
    db_ids = {str(c[0]) for c in env.chunks(repo_id)}
    assert set(stored["ids"]) == db_ids
    assert coll.count() == len(db_ids)
    assert all(m["repository_id"] == repo_id and m["job_id"] == job_id for m in stored["metadatas"])
    assert "indexed" in env.job(job_id)[1]


def test_search_returns_schema_valid_relevant_results(env):
    env.set_source(URL_A, ALPHA_V1)
    repo_id, job_id = env.ingest(URL_A)

    res = env.search(repo_id, "invoice total price items")
    assert res.status_code == 200
    body = res.json()
    assert body and res.headers["X-Index-Job-Id"] == str(job_id)
    top = body[0]
    for key in ("id", "repository_id", "file_path", "chunk_index", "chunk_type",
                "symbol_name", "start_line", "end_line", "content", "score"):
        assert key in top
    assert top["symbol_name"] == "compute_invoice_total"
    assert "return sum" in top["content"]
    assert top["repository_id"] == repo_id
    scores = [r["score"] for r in body]
    assert scores == sorted(scores, reverse=True)


def test_search_is_scoped_to_one_repository(env):
    env.set_source(URL_A, ALPHA_V1)
    env.set_source(URL_B, BETA_V1)
    repo_a, _ = env.ingest(URL_A)
    repo_b, _ = env.ingest(URL_B)
    ids_a = {c[0] for c in env.chunks(repo_a)}
    ids_b = {c[0] for c in env.chunks(repo_b)}
    assert ids_a.isdisjoint(ids_b)

    res_a = env.search(repo_a, "weather forecast temperature city", top_k=50).json()
    assert {r["id"] for r in res_a} <= ids_a
    assert all(r["repository_id"] == repo_a for r in res_a)

    res_b = env.search(repo_b, "weather forecast temperature city", top_k=50).json()
    assert res_b and {r["id"] for r in res_b} <= ids_b
    assert res_b[0]["symbol_name"] in ("WeatherForecastClient", "fetch_temperature")


def test_top_k_is_respected_and_capped_by_available_chunks(env):
    env.set_source(URL_A, ALPHA_V1)
    repo_id, _ = env.ingest(URL_A)
    total = len(env.chunks(repo_id))
    assert len(env.search(repo_id, "email", top_k=1).json()) == 1
    assert len(env.search(repo_id, "email", top_k=50).json()) == total


# ---------------------------------------------------------------- query validation / states

@pytest.mark.parametrize("payload", [
    {"query": ""},
    {"query": "   "},
    {"query": "ok", "top_k": 0},
    {"query": "ok", "top_k": 51},
    {"query": "ok", "top_k": "many"},
    {},
    {"query": "x" * 2001},
])
def test_invalid_search_requests_are_422(env, payload):
    env.set_source(URL_A, ALPHA_V1)
    repo_id, _ = env.ingest(URL_A)
    assert env.client.post(f"/repositories/{repo_id}/search", json=payload).status_code == 422


def test_search_unknown_repository_is_404(env):
    assert env.search(9999, "anything").status_code == 404


def test_search_before_any_successful_ingestion_is_409_not_empty_list(env):
    env.set_source(URL_A, ALPHA_V1)
    env.monkeypatch.setattr(vector_store, "index_chunks",
                            lambda *a, **k: (_ for _ in ()).throw(vector_store.VectorStoreError("boom")))
    repo_id, job_id = env.ingest(URL_A)
    assert env.job(job_id)[0] == "failed"
    res = env.search(repo_id, "anything")
    assert res.status_code == 409
    assert "failed" in res.json()["detail"]


def test_repository_without_python_chunks_succeeds_and_search_is_explicitly_empty(env):
    env.set_source(URL_A, "just text, not python\n", filename="README.md")
    repo_id, job_id = env.ingest(URL_A)
    status, log = env.job(job_id)
    assert status == "succeeded" and "nothing to index" in log
    assert env.collections() == []
    res = env.search(repo_id, "anything")
    assert res.status_code == 200 and res.json() == []


# ---------------------------------------------------------------- failure surfacing

def test_embedding_failure_fails_job_with_real_error_and_persists_nothing(env):
    class BrokenModel:
        def encode(self, *a, **k):
            raise RuntimeError("model exploded")

    env.set_source(URL_A, ALPHA_V1)
    env.monkeypatch.setattr(embeddings, "_model", BrokenModel())
    repo_id, job_id = env.ingest(URL_A)

    status, log = env.job(job_id)
    assert status == "failed"
    assert "vector indexing" in log and "EmbeddingError" in log and "model exploded" in log
    assert env.chunks(repo_id) == []           # transaction rolled back
    assert env.collections() == []             # no half-built collection left behind
    assert env.search(repo_id, "x").status_code == 409


def test_missing_embedding_dependency_is_reported_not_swallowed(env):
    env.set_source(URL_A, ALPHA_V1)
    env.monkeypatch.setattr(embeddings, "_model", None)
    env.monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", None)  # import fails
    _, job_id = env.ingest(URL_A)
    status, log = env.job(job_id)
    assert status == "failed" and "Failed to load embedding model" in log


def test_vector_store_failure_fails_job(env):
    env.set_source(URL_A, ALPHA_V1)

    def boom(*a, **k):
        raise vector_store.VectorStoreError("chroma unavailable")

    env.monkeypatch.setattr(vector_store, "index_chunks", boom)
    repo_id, job_id = env.ingest(URL_A)
    status, log = env.job(job_id)
    assert status == "failed" and "chroma unavailable" in log
    assert env.chunks(repo_id) == []


def test_search_reports_503_when_embedding_model_unavailable(env):
    env.set_source(URL_A, ALPHA_V1)
    repo_id, _ = env.ingest(URL_A)

    class BrokenModel:
        def encode(self, *a, **k):
            raise RuntimeError("gpu on fire")

    env.monkeypatch.setattr(embeddings, "_model", BrokenModel())
    res = env.search(repo_id, "invoice")
    assert res.status_code == 503 and "Embedding model unavailable" in res.json()["detail"]


def test_search_reports_503_when_index_is_missing_not_empty_list(env):
    env.set_source(URL_A, ALPHA_V1)
    repo_id, job_id = env.ingest(URL_A)
    vector_store._get_client().delete_collection(vector_store.collection_name(repo_id, job_id))
    res = env.search(repo_id, "invoice")
    assert res.status_code == 503 and "re-run ingestion" in res.json()["detail"]


def test_search_reports_503_when_index_out_of_sync_with_database(env):
    env.set_source(URL_A, ALPHA_V1)
    repo_id, job_id = env.ingest(URL_A)
    db = env.Session()
    db.query(CodeChunk).filter(CodeChunk.repository_id == repo_id).update({"repository_id": 12345})
    db.commit()
    db.close()
    # chunk rows no longer belong to the repo, so the DB says "no chunks" -> explicit empty;
    # put one back to simulate real drift: rows exist but with different ids than the index
    db = env.Session()
    db.add(CodeChunk(repository_id=repo_id, file_path="x.py", chunk_index=0, chunk_type="module_level",
                     start_line=1, end_line=1, content="x = 1", id=99999))
    db.commit()
    db.close()
    res = env.search(repo_id, "invoice")
    assert res.status_code == 503 and "out of sync" in res.json()["detail"]


# ---------------------------------------------------------------- re-ingestion

def test_reingestion_replaces_stale_vectors_and_results(env):
    env.set_source(URL_A, ALPHA_V1)
    repo_id, job1 = env.ingest(URL_A)
    # simulate a leftover from the old (pre-fix) code: a legacy un-scoped collection
    vector_store._get_client().create_collection("repo_%d" % repo_id)
    assert env.search(repo_id, "invoice total price").json()[0]["symbol_name"] == "compute_invoice_total"

    env.set_source(URL_A, ALPHA_V2)  # same repo, new content
    _, job2 = env.ingest(URL_A)
    assert job2 != job1 and env.job(job2)[0] == "succeeded"

    results = env.search(repo_id, "invoice total price email", top_k=50).json()
    contents = " ".join(r["content"] for r in results)
    assert "compute_invoice_total" not in contents and "send_email_notification" not in contents
    assert "render_dashboard_widget" in contents
    assert {r["id"] for r in results} == {c[0] for c in env.chunks(repo_id)}
    assert env.collections() == [vector_store.collection_name(repo_id, job2)]


def test_failed_reingestion_keeps_previous_index_and_reports_failure(env):
    env.set_source(URL_A, ALPHA_V1)
    repo_id, job1 = env.ingest(URL_A)
    before = {c[0] for c in env.chunks(repo_id)}

    class BrokenModel:
        def encode(self, *a, **k):
            raise RuntimeError("model exploded")

    good_model = embeddings._model
    env.set_source(URL_A, ALPHA_V2)
    env.monkeypatch.setattr(embeddings, "_model", BrokenModel())
    _, job2 = env.ingest(URL_A)

    status, log = env.job(job2)
    assert status == "failed" and "model exploded" in log     # failure reported truthfully

    # recoverable: previous data and previous index are intact...
    assert {c[0] for c in env.chunks(repo_id)} == before
    assert env.collections() == [vector_store.collection_name(repo_id, job1)]
    env.monkeypatch.setattr(embeddings, "_model", good_model)  # "fix" the problem
    res = env.search(repo_id, "invoice total price")
    assert res.status_code == 200
    assert res.json()[0]["symbol_name"] == "compute_invoice_total"
    # ...and the response says which job's index answered, not the failed one
    assert res.headers["X-Index-Job-Id"] == str(job1)

    # a retry after fixing the problem succeeds and replaces the index
    _, job3 = env.ingest(URL_A)
    assert env.job(job3)[0] == "succeeded"
    assert env.collections() == [vector_store.collection_name(repo_id, job3)]
    assert "render_dashboard_widget" in " ".join(r["content"] for r in env.search(repo_id, "dashboard widget").json())


def test_stale_collection_cleanup_failure_does_not_fail_job_or_leak_results(env):
    env.set_source(URL_A, ALPHA_V1)
    repo_id, job1 = env.ingest(URL_A)
    env.set_source(URL_A, ALPHA_V2)
    env.monkeypatch.setattr(vector_store, "delete_repo_collections", lambda *a, **k: ["repo_x_job_1"])
    _, job2 = env.ingest(URL_A)
    assert env.job(job2)[0] == "succeeded"
    # old collection still exists but search reads only the latest succeeded job's index
    assert vector_store.collection_name(repo_id, job1) in env.collections()
    contents = " ".join(r["content"] for r in env.search(repo_id, "invoice total email", top_k=50).json())
    assert "compute_invoice_total" not in contents


# ---------------------------------------------------------------- unit-level

def test_embed_texts_rejects_bad_model_output():
    class BadShape:
        def encode(self, texts, **k):
            import numpy as np
            return np.zeros((1, 4))

    embeddings._model = BadShape()
    with pytest.raises(embeddings.EmbeddingError, match="shape"):
        embeddings.embed_texts(["a", "b"])

    class NaNModel:
        def encode(self, texts, **k):
            import numpy as np
            return np.full((len(texts), 4), np.nan)

    embeddings._model = NaNModel()
    with pytest.raises(embeddings.EmbeddingError, match="non-finite"):
        embeddings.embed_texts(["a"])


def test_index_chunks_requires_persisted_ids():
    class Unsaved:
        id = None
        repository_id = 1
        content = "x"
        file_path = "a.py"
        symbol_name = None
        chunk_type = "module_level"

    with pytest.raises(vector_store.VectorStoreError, match="database id"):
        vector_store.index_chunks(1, 1, [Unsaved()])


def test_sanitize_error_masks_url_credentials_and_env_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-value")
    msg = sanitize_error(RuntimeError(
        "could not connect to postgresql://postgres:hunter2@localhost/db using sk-super-secret-value\nline2"))
    assert "hunter2" not in msg and "sk-super-secret-value" not in msg
    assert "RuntimeError" in msg and "\n" not in msg
    assert len(sanitize_error(RuntimeError("x" * 5000))) <= 500
