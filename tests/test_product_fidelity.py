import pytest
from fastapi.testclient import TestClient

from main import app, runtime


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


def _create_output(client, monkeypatch):
    class FakeProvider:
        async def generate(self, request):
            return {"image_url": "https://example.test/generated.png", "provider": "test-provider", "fidelity_score": 0.8}

    monkeypatch.setattr("services.ai_photoshooting.get_provider", lambda: FakeProvider())
    project = client.post("/api/creative/projects", json={"name": "Fidelity Project", "project_type": "photoshoot"}, headers={"X-Internal-API-Key": "test-internal-key"}).json()
    job = client.post("/api/creative/jobs", json={"project_id": project["id"], "job_type": "image_generation"}, headers={"X-Internal-API-Key": "test-internal-key"}).json()
    generation = client.post(f"/api/creative/jobs/{job['id']}/generate-image", json={"prompt": "make image"}, headers={"X-Internal-API-Key": "test-internal-key"})
    assert generation.status_code == 200
    return generation.json()["asset_id"], client.get(f"/api/creative/outputs?job_id={job['id']}", headers={"X-Internal-API-Key": "test-internal-key"}).json()[0]["id"]


def test_product_fidelity_unavailable_without_omniroute(client, monkeypatch):
    _, output_id = _create_output(client, monkeypatch)
    monkeypatch.setattr(runtime, "omniroute", None)

    response = client.post(
        f"/api/creative/outputs/{output_id}/fidelity-check",
        json={"original_image_url": "https://example.test/original.png", "generated_image_url": "https://example.test/generated.png"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"


def test_product_fidelity_passes_without_critical_deviations(client, monkeypatch):
    _, output_id = _create_output(client, monkeypatch)

    class FakeOmniRoute:
        async def analyze_images(self, prompt, image_urls, model="gemini-2.5-flash"):
            assert len(image_urls) == 2
            return {
                "visual_quality": 91,
                "product_fidelity": 93,
                "model_consistency": 88,
                "scene_quality": 85,
                "technical_quality": 90,
                "deviations": ["minor lighting difference"],
                "critical_deviations": [],
                "summary": "acceptable",
            }

    monkeypatch.setattr(runtime, "omniroute", FakeOmniRoute())
    response = client.post(
        f"/api/creative/outputs/{output_id}/fidelity-check",
        json={"original_image_url": "https://example.test/original.png", "generated_image_url": "https://example.test/generated.png"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["publishable"] is True
    assert body["product_fidelity"] == 93

    outputs = client.get("/api/creative/outputs", headers={"X-Internal-API-Key": "test-internal-key"}).json()
    updated = next(item for item in outputs if item["id"] == output_id)
    assert updated["approval_status"] == "quality_check"
    assert updated["fidelity_score"] == 93


def test_product_fidelity_blocks_critical_deviations(client, monkeypatch):
    _, output_id = _create_output(client, monkeypatch)

    class FakeOmniRoute:
        async def analyze_images(self, prompt, image_urls, model="gemini-2.5-flash"):
            return {
                "visual_quality": 70,
                "product_fidelity": 40,
                "model_consistency": 60,
                "scene_quality": 65,
                "technical_quality": 75,
                "deviations": ["wrong color", "wrong cut"],
                "critical_deviations": ["wrong color"],
                "summary": "not acceptable",
            }

    monkeypatch.setattr(runtime, "omniroute", FakeOmniRoute())
    response = client.post(
        f"/api/creative/outputs/{output_id}/fidelity-check",
        json={"original_image_url": "https://example.test/original.png", "generated_image_url": "https://example.test/generated.png"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["publishable"] is False
    assert body["critical_deviations"] == ["wrong color"]

    outputs = client.get("/api/creative/outputs", headers={"X-Internal-API-Key": "test-internal-key"}).json()
    updated = next(item for item in outputs if item["id"] == output_id)
    assert updated["approval_status"] == "review_required"
