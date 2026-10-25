import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from backend.config import load_env

load_env()  # explicit, cwd-independent .env path; must run before the modules below read the environment

from backend.routers import repo  # noqa: E402

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

# Register routers
app.include_router(repo.router)


@app.get("/health", response_class=JSONResponse)
async def health_check():
    """Simple liveness endpoint – returns a JSON payload.
    We will add more routes later, but this proves the server starts.
    """
    return {"status": "ok"}
