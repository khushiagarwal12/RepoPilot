"""OpenAPI / routing consistency guards (regression for the duplicate search route)."""
import ast
import warnings
from collections import Counter
from pathlib import Path

from fastapi.routing import APIRoute

from backend.main import app

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


def test_each_method_and_path_is_registered_once():
    routes = [
        (method, r.path)
        for r in app.routes
        if isinstance(r, APIRoute)
        for method in r.methods
    ]
    dupes = [k for k, n in Counter(routes).items() if n > 1]
    assert dupes == []


def test_exactly_one_search_route():
    search_routes = [r for r in app.routes if isinstance(r, APIRoute) and r.path.endswith("/search")]
    assert len(search_routes) == 1 and search_routes[0].methods == {"POST"}


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
