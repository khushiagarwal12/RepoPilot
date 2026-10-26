"""Tiny in-memory sliding-window rate limiter (per client IP, per endpoint group).

Good enough for a single API process. Behind a reverse proxy the client IP is the
proxy's unless it forwards it; use the proxy's own limiter in production.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

_hits: dict = defaultdict(deque)
WINDOW_SECONDS = 60.0


def reset() -> None:
    _hits.clear()


def limit_per_minute() -> int:
    try:
        return int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))
    except ValueError:
        return 30


def rate_limit(group: str):
    def dependency(request: Request) -> None:
        limit = limit_per_minute()
        if limit <= 0:
            return
        key = (group, request.client.host if request.client else "unknown")
        now = time.monotonic()
        q = _hits[key]
        while q and now - q[0] > WINDOW_SECONDS:
            q.popleft()
        if len(q) >= limit:
            retry = max(1, int(WINDOW_SECONDS - (now - q[0])))
            raise HTTPException(status_code=429, detail="Too many requests; slow down.", headers={"Retry-After": str(retry)})
        q.append(now)

    return dependency
