"""Compare vector vs hybrid retrieval on labelled queries (run against a RUNNING backend).

    python scratch/eval_retrieval.py --repo-id 1            # uses the built-in psf/requests sample set
    python scratch/eval_retrieval.py --repo-id 1 --queries my_queries.json --k 5

my_queries.json: [{"query": "...", "expect": ["sessions.py", "resolve_redirects"]}, ...]
A result is relevant if ANY expected substring appears in its file_path or symbol_name.
Prints hit@k and MRR per mode. Use it to decide whether RETRIEVAL_MODE=hybrid helps.
"""
import argparse
import json

import requests

SAMPLE = [  # sample labels for psf/requests; adjust if your checkout differs
    {"query": "how are HTTP redirects followed", "expect": ["resolve_redirects", "sessions.py"]},
    {"query": "basic authentication header", "expect": ["HTTPBasicAuth", "auth.py", "_basic_auth_str"]},
    {"query": "parse JSON response body", "expect": ["models.py", "json"]},
    {"query": "connection pooling and retries adapter", "expect": ["HTTPAdapter", "adapters.py"]},
    {"query": "cookie jar handling", "expect": ["cookies.py", "RequestsCookieJar"]},
    {"query": "proxy settings from environment", "expect": ["get_environ_proxies", "utils.py", "should_bypass_proxies"]},
    {"query": "exception for connection timeout", "expect": ["exceptions.py", "Timeout"]},
]


def relevant(result, expect):
    hay = f"{result.get('file_path','')} {result.get('symbol_name') or ''}"
    return any(e.lower() in hay.lower() for e in expect)


def score(ranked_flags):
    """ranked_flags: list of bool per query-result list -> (hit@k, MRR)."""
    hits = sum(1 for flags in ranked_flags if any(flags))
    mrr = sum((1 / (flags.index(True) + 1)) if True in flags else 0 for flags in ranked_flags)
    n = max(len(ranked_flags), 1)
    return hits / n, mrr / n


def evaluate(base, repo_id, queries, mode, k):
    flags = []
    for q in queries:
        r = requests.post(f"{base}/repositories/{repo_id}/search",
                          json={"query": q["query"], "top_k": k, "mode": mode}, timeout=60)
        r.raise_for_status()
        flags.append([relevant(x, q["expect"]) for x in r.json()])
    return score(flags)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--repo-id", type=int, required=True)
    p.add_argument("--queries")
    p.add_argument("--k", type=int, default=5)
    a = p.parse_args()
    queries = json.load(open(a.queries, encoding="utf-8")) if a.queries else SAMPLE
    for mode in ("vector", "hybrid"):
        hit, mrr = evaluate(a.base_url, a.repo_id, queries, mode, a.k)
        print(f"{mode:7s} hit@{a.k}={hit:.2f}  MRR={mrr:.2f}  ({len(queries)} queries)")


if __name__ == "__main__":
    main()
