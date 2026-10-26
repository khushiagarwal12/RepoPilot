"""Structured (JSON) logging with secret redaction and per-request ids."""
from __future__ import annotations

import contextvars
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from backend.utils.errors import redact_text

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
_HANDLER_NAME = "repopilot-json"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id_var.get(),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact_text(json.dumps(payload, ensure_ascii=False))  # redact the final line: nothing leaks


def configure_logging() -> None:
    """Idempotent: attach one JSON handler to the root logger."""
    root = logging.getLogger()
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    if any(getattr(h, "name", "") == _HANDLER_NAME for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.name = _HANDLER_NAME
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, log one access line per request, expose X-Request-ID."""

    async def dispatch(self, request: Request, call_next):
        rid = uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - convert to a safe 500 that still carries the request id
            logging.getLogger("repopilot.error").error(
                "Unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path, exc_info=exc)
            response = JSONResponse(status_code=500, content={"detail": "Internal server error", "request_id": rid})
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        logging.getLogger("repopilot.access").info(
            "%s %s -> %s in %dms", request.method, request.url.path, response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return response
