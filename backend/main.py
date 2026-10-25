import os
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from backend.routers import repo

load_dotenv()

app = FastAPI(title="RepoPilot Backend")

# Register routers
app.include_router(repo.router)


@app.get("/health", response_class=JSONResponse)
async def health_check():
    """Simple liveness endpoint – returns a JSON payload.
    We will add more routes later, but this proves the server starts.
    """
    return {"status": "ok"}
