from fastapi.testclient import TestClient

from main import app


def test_internal_api_key_required_for_protected_routes(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    client = TestClient(app)

    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/export/channels").status_code == 401


def test_internal_api_key_exempts_health_endpoint(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    client = TestClient(app)

    assert client.get("/api/health").status_code == 200


def test_browser_session_authenticates_api(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"api_key": "test-internal-key"})
        assert login.status_code == 200
        assert client.get("/api/stats").status_code == 200


def test_invalid_browser_login_is_rejected(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    client = TestClient(app)
    assert client.post("/api/auth/login", json={"api_key": "wrong-key"}).status_code == 401
