import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    import asyncio
    import database
    import services.asset_manager as asset_manager

    database.DATABASE_URL = ""
    database.DATABASE_PATH = tmp_path / "test.db"
    asyncio.run(database.init_db())

    asset_root = tmp_path / "assets"
    asset_manager.ASSET_BASE_DIR = asset_root
    asset_manager.ORIGINAL_DIR = asset_root / "original"
    asset_manager.THUMBNAIL_DIR = asset_root / "thumbnails"
    asset_manager._ensure_dirs()
    return TestClient(app)


def test_agent_tools_are_registered(client):
    response = client.get("/api/agent-tools", headers={"X-Internal-API-Key": "test-internal-key"})
    assert response.status_code == 200
    names = {tool["name"] for tool in response.json()}
    assert "search_products" in names
    assert "request_approval" in names


def test_agent_profile_conversation_run_and_tool_execution(client):
    profile_response = client.post(
        "/api/agent-profiles",
        json={"name": "Creative Agent", "allowed_tools": ["search_products", "analyze_product"]},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert profile_response.status_code == 201
    profile = profile_response.json()

    conversation_response = client.post(
        "/api/agent-conversations",
        json={"agent_profile_id": profile["id"]},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert conversation_response.status_code == 201
    conversation = conversation_response.json()

    run_response = client.post(
        "/api/agent-runs",
        json={"conversation_id": conversation["id"], "agent_profile_id": profile["id"], "input_data": {"task": "audit"}},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert run_response.status_code == 201
    run = run_response.json()

    tool_response = client.post(
        f"/api/agent-runs/{run['id']}/tools",
        json={"tool_name": "search_products", "tool_input": {"limit": 1}},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert tool_response.status_code == 200
    assert tool_response.json()["status"] == "success"
    assert "products" in tool_response.json()["output"]

    calls = client.get(f"/api/agent-runs/{run['id']}/tool-calls", headers={"X-Internal-API-Key": "test-internal-key"})
    assert calls.status_code == 200
    assert calls.json()[0]["tool_name"] == "search_products"

    invalid = client.post(
        f"/api/agent-runs/{run['id']}/tools",
        json={"tool_name": "search_products", "tool_input": {"limit": 0}},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert invalid.status_code == 400

    disallowed = client.post(
        f"/api/agent-runs/{run['id']}/tools",
        json={"tool_name": "create_bundle", "tool_input": {"bundle_sku": "B", "title": "T", "component_skus": ["S"]}},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert disallowed.status_code == 400

    completed = client.patch(
        f"/api/agent-runs/{run['id']}",
        json={"status": "completed", "output_data": {"done": True}},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"


def test_agent_approval_lifecycle(client):
    profile = client.post("/api/agent-profiles", json={"name": "Approval Agent", "allowed_tools": ["request_approval"]}, headers={"X-Internal-API-Key": "test-internal-key"}).json()
    conversation = client.post("/api/agent-conversations", json={"agent_profile_id": profile["id"]}, headers={"X-Internal-API-Key": "test-internal-key"}).json()
    run = client.post("/api/agent-runs", json={"conversation_id": conversation["id"], "agent_profile_id": profile["id"]}, headers={"X-Internal-API-Key": "test-internal-key"}).json()

    approval = client.post(
        "/api/agent-approvals",
        json={"run_id": run["id"], "target_type": "asset", "target_id": "asset-1", "reason": "publish"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert approval.status_code == 201
    approval_id = approval.json()["id"]

    listed = client.get(f"/api/agent-approvals?run_id={run['id']}", headers={"X-Internal-API-Key": "test-internal-key"})
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == approval_id

    reviewed = client.patch(
        f"/api/agent-approvals/{approval_id}",
        json={"approved": True, "reviewed_by": "admin", "reason": "ok"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "approved"


def test_agent_publish_requires_approved_asset(client):
    upload = client.post(
        "/api/assets",
        data={"asset_type": "image", "approval_status": "approved"},
        files={"file": ("approved.png", io.BytesIO(b"approved-bytes"), "image/png")},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert upload.status_code == 201
    asset_id = upload.json()["id"]

    profile = client.post("/api/agent-profiles", json={"name": "Publisher", "allowed_tools": ["publish_approved_asset"]}, headers={"X-Internal-API-Key": "test-internal-key"}).json()
    conversation = client.post("/api/agent-conversations", json={"agent_profile_id": profile["id"]}, headers={"X-Internal-API-Key": "test-internal-key"}).json()
    run = client.post("/api/agent-runs", json={"conversation_id": conversation["id"], "agent_profile_id": profile["id"]}, headers={"X-Internal-API-Key": "test-internal-key"}).json()

    result = client.post(
        f"/api/agent-runs/{run['id']}/tools",
        json={"tool_name": "publish_approved_asset", "tool_input": {"asset_id": asset_id}},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert result.status_code == 200
    assert result.json()["output"]["status"] == "published"

    detail = client.get(f"/api/assets/{asset_id}", headers={"X-Internal-API-Key": "test-internal-key"})
    assert detail.json()["approval_status"] == "published"


def test_agent_omniroute_selection(client, monkeypatch):
    class FakeModel:
        model_name = "Test Model"
        model_category = "text"
        capabilities = ["text"]
        input_price_per_1k = 0.1
        output_price_per_1k = 0.2
        image_price_per_image = 0.0

    class FakeRegistry:
        def get_model(self, model_id):
            return FakeModel()

    class FakeEngine:
        def select_provider(self, workflow_name, required_capability=None, max_cost=None):
            assert workflow_name == "creative-agent"
            return "provider-1", "model-1"

    fake_runtime = SimpleNamespace(omniroute_routing_engine=FakeEngine(), omniroute_model_registry=FakeRegistry(), omniroute_cost_calculator=None)
    monkeypatch.setattr("main.runtime", fake_runtime)

    response = client.post(
        "/api/agent-omniroute/select",
        json={"workflow_name": "creative-agent", "required_capability": "text", "input_tokens": 10, "output_tokens": 20},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert response.status_code == 200
    assert response.json()["provider_id"] == "provider-1"
    assert response.json()["model_id"] == "model-1"


def test_agent_generate_image_uses_provider(client, monkeypatch):
    class FakeProvider:
        async def generate(self, request):
            assert request["prompt"] == "make image"
            return {"image_url": "https://example.test/image.png"}

    monkeypatch.setattr("services.ai_photoshooting.get_provider", lambda: FakeProvider())

    profile = client.post("/api/agent-profiles", json={"name": "Image Agent", "allowed_tools": ["generate_image"]}, headers={"X-Internal-API-Key": "test-internal-key"}).json()
    conversation = client.post("/api/agent-conversations", json={"agent_profile_id": profile["id"]}, headers={"X-Internal-API-Key": "test-internal-key"}).json()
    run = client.post("/api/agent-runs", json={"conversation_id": conversation["id"], "agent_profile_id": profile["id"]}, headers={"X-Internal-API-Key": "test-internal-key"}).json()

    result = client.post(
        f"/api/agent-runs/{run['id']}/tools",
        json={"tool_name": "generate_image", "tool_input": {"prompt": "make image"}},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert result.status_code == 200
    assert result.json()["output"]["image_url"] == "https://example.test/image.png"
