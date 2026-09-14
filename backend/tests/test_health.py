from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint_reports_database_connection():
    with TestClient(app) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected"}


def test_summary_confirms_v1_safety_boundaries():
    with TestClient(app) as client:
        response = client.get("/api/v1/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["territory"] == "Val-de-Marne (94)"
    assert body["external_connectors_enabled"] == 0
    assert body["contact_automation_enabled"] is False


def test_local_frontend_origins_are_allowed():
    with TestClient(app) as client:
        response = client.get("/api/v1/health", headers={"Origin": "http://127.0.0.1:5173"})
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
