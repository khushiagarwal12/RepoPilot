"""OpenAPI / routing consistency guards (regression for the duplicate search route)."""
import ast
import warnings
from collections import Counter
from pathlib import Path

from fastapi.routing import APIRoute

from backend.main import app
from backend.routers import repo as repo_module

REPO_ROUTER = Path(__file__).resolve().parent.parent / "backend" / "routers" / "repo.py"


def test_openapi_generates_without_duplicate_operation_id_warning():
    app.openapi_schema = None  # force regeneration so warnings are emitted again
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        schema = app.openapi()
    messages = [str(w.message) for w in caught]
    assert not [m for m in messages if "Duplicate Operation ID" in m], messages

    op_ids = [
        op["operationId"]
        for path in schema["paths"].values()
        for op in path.values()
        if "operationId" in op
    ]
    assert len(op_ids) == len(set(op_ids))


def _registrations(routes):
    """(method, path) for every direct APIRoute in ``routes``."""
    return [(m, r.path) for r in routes if isinstance(r, APIRoute) for m in r.methods]


def _duplicates(routes):
    return [k for k, n in Counter(_registrations(routes)).items() if n > 1]


# (method, path relative to the router prefix) that the repository router must register.
EXPECTED_REPO_ROUTES = {
    ("GET", "/"),
    ("POST", "/"),
    ("GET", "/{repo_id}"),
    ("GET", "/{repo_id}/jobs"),
    ("GET", "/{repo_id}/definitions"),
    ("GET", "/{repo_id}/chunks"),
    ("POST", "/{repo_id}/search"),
    ("POST", "/{repo_id}/ask"),
}


def test_each_method_and_path_is_registered_once():
    # Newer FastAPI (0.162+) keeps included routers as lazy wrappers, so app.routes no
    # longer lists their APIRoute objects. Check the app's direct routes AND the router
    # object itself, whose route list is the same in every FastAPI version.
    assert _duplicates(app.routes) == []
    assert _duplicates(repo_module.router.routes) == []

    # Explicit contract: every expected method/path exists on the router (so the duplicate
    # check above can never pass vacuously, whatever FastAPI does with app.routes).
    prefix = repo_module.router.prefix
    registered = set(_registrations(repo_module.router.routes))
    missing = {(m, prefix + path) for m, path in EXPECTED_REPO_ROUTES} - registered
    assert missing == set()


def test_exactly_one_search_route():
    # 1) Registration level: exactly one POST /search on the router itself (version independent).
    router_search = [r for r in repo_module.router.routes if isinstance(r, APIRoute) and r.path.endswith("/search")]
    assert len(router_search) == 1 and router_search[0].methods == {"POST"}

    # 2) Older FastAPI copies included routes into app.routes: there, still require at most one.
    app_search = [r for r in app.routes if isinstance(r, APIRoute) and r.path.endswith("/search")]
    assert len(app_search) <= 1 and all(r.methods == {"POST"} for r in app_search)

    # 3) Application level: the app really exposes it, as a single POST operation, in OpenAPI.
    paths = app.openapi()["paths"]
    search_paths = [p for p in paths if p.endswith("/search")]
    assert search_paths == ["/repositories/{repo_id}/search"]
    assert set(paths[search_paths[0]]) == {"post"}


def test_router_module_has_no_duplicate_function_definitions_or_imports():
    tree = ast.parse(REPO_ROUTER.read_text(encoding="utf-8"))
    funcs = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert [f for f, n in Counter(funcs).items() if n > 1] == []

    imported = Counter()
    for n in tree.body:
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                imported[(n.module, a.name)] += 1
        elif isinstance(n, ast.Import):
            for a in n.names:
                imported[(None, a.name)] += 1
    assert [k for k, c in imported.items() if c > 1] == []


def test_search_schema_matches_what_the_endpoint_returns():
    schema = app.openapi()
    result = schema["components"]["schemas"]["SearchResult"]
    assert {"id", "repository_id", "file_path", "chunk_index", "chunk_type", "symbol_name",
            "start_line", "end_line", "content", "score"} <= set(result["properties"])
    request = schema["components"]["schemas"]["SearchRequest"]["properties"]
    assert request["top_k"]["minimum"] == 1 and request["top_k"]["maximum"] == 50
    assert request["query"]["minLength"] == 1
