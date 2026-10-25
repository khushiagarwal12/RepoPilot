from fastapi.testclient import TestClient
from backend.main import app

client = TestClient(app)


def test_health_endpoint_contract():
    """Verify that the /health endpoint adheres to the expected API contract."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)
    assert data.get("status") == "ok"
