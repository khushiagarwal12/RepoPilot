import os
from fastapi.testclient import TestClient
from backend.main import app

client = TestClient(app)

def test_health_endpoint():
    """Ensure the /health endpoint returns a JSON payload with status 'ok'."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
