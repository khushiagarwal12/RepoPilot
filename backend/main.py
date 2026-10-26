import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.config import load_env

load_env()  # explicit, cwd-independent .env path; must run before the modules below read the environment

from backend.logging_config import RequestContextMiddleware, configure_logging, request_id_var  # noqa: E402
from backend.routers import repo  # noqa: E402

configure_logging()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        recovered = repo.recover_interrupted_jobs()
        if recovered:
            logger.warning("Marked %d interrupted ingestion job(s) as failed", recovered)
    except Exception as exc:  # noqa: BLE001 - startup must not crash, but the cause is logged
        logger.error("Could not recover interrupted jobs: %s", type(exc).__name__)
    yield


app = FastAPI(title="RepoPilot Backend", lifespan=lifespan)

_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
    expose_headers=["X-Index-Job-Id", "X-Request-ID", "Retry-After"],
)
app.add_middleware(RequestContextMiddleware)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Never leak tracebacks or internals to clients; log details (redacted) server-side."""
    logger.error("Unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error", "request_id": request_id_var.get()})

# Register routers
app.include_router(repo.router)


@app.get("/health", response_class=JSONResponse)
async def health_check():
    """Simple liveness endpoint – returns a JSON payload.
    We will add more routes later, but this proves the server starts.
    """
    return {"status": "ok"}
