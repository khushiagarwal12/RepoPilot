from fastapi import FastAPI
from fastapi.responses import JSONResponse

from backend.config import load_env

load_env()  # explicit, cwd-independent .env path; must run before the modules below read the environment

from backend.routers import repo  # noqa: E402

app = FastAPI(title="RepoPilot Backend")

# Register routers
app.include_router(repo.router)


@app.get("/health", response_class=JSONResponse)
async def health_check():
    """Simple liveness endpoint – returns a JSON payload.
    We will add more routes later, but this proves the server starts.
    """
    return {"status": "ok"}
