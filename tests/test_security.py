import base64
import hashlib
import hmac
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import main
from main import app, require_admin


def _admin_request(cookie: str | None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"cookie", f"admin_session={cookie}".encode())] if cookie else [],
            "query_string": b"",
        }
    )


def test_internal_api_key_required_for_protected_routes(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    client = TestClient(app)

    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/export/channels").status_code == 401


def test_internal_api_key_exempts_health_endpoint(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    client = TestClient(app)

    assert client.get("/api/health").status_code == 200


def test_admin_session_login_returns_admin_session_cookie(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")

    client = TestClient(app)
    response = client.post(
        "/api/admin/session/login",
        data={"api_key": "test-internal-key"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    admin_session = response.cookies.get("admin_session")
    assert admin_session
    assert "." in admin_session


def test_admin_session_login_rejects_invalid_api_key(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")

    client = TestClient(app)
    response = client.post(
        "/api/admin/session/login",
        data={"api_key": "wrong-api-key"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )

    assert response.status_code == 401
    assert response.cookies.get("admin_session") is None


def test_photoshoot_start_uses_existing_provider(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")

    class FakeProvider:
        async def generate(self, request):
            assert request["prompt"] == "test prompt"
            return {"image_url": "https://example.test/generated.png"}

    monkeypatch.setattr(main, "get_photoshoot_provider", lambda: FakeProvider())
    client = TestClient(app)
    response = client.post(
        "/api/photoshoot/start",
        json={"prompt": "test prompt"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "processing"
    assert response.json()["image_url"] == "https://example.test/generated.png"
    assert response.json()["shoot_id"]


@pytest.mark.asyncio
async def test_require_admin_requires_session_cookie():
    with pytest.raises(HTTPException) as exc_info:
        await require_admin(_admin_request(None))

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_require_admin_rejects_invalid_session():
    with pytest.raises(HTTPException) as exc_info:
        await require_admin(_admin_request("invalid.token"))

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_require_admin_accepts_valid_admin_session(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")

    payload = {"sub": "admin", "iat": 123}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    signature = hmac.new(
        b"test-session-secret",
        payload_b64.encode(),
        hashlib.sha256,
    ).hexdigest()
    token = f"{payload_b64}.{signature}"

    result = await require_admin(_admin_request(token))

    assert result["sub"] == "admin"
    assert result["role"] == "admin"
