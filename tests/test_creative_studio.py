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


def test_creative_project_job_image_output_and_video_scene(client, monkeypatch):
    class FakeProvider:
        async def generate(self, request):
            assert request["prompt"] == "make image"
            return {"image_url": "https://example.test/generated.png", "provider": "test-provider", "fidelity_score": 0.9}

    monkeypatch.setattr("services.ai_photoshooting.get_provider", lambda: FakeProvider())

    project_response = client.post(
        "/api/creative/projects",
        json={"name": "Spring Shoot", "project_type": "photoshoot"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert project_response.status_code == 201
    project = project_response.json()

    job_response = client.post(
        "/api/creative/jobs",
        json={"project_id": project["id"], "job_type": "image_generation"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert job_response.status_code == 201
    job = job_response.json()

    generation = client.post(
        f"/api/creative/jobs/{job['id']}/generate-image",
        json={"prompt": "make image"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert generation.status_code == 200
    assert generation.json()["status"] == "generated"
    asset_id = generation.json()["asset_id"]

    outputs = client.get(f"/api/creative/outputs?job_id={job['id']}", headers={"X-Internal-API-Key": "test-internal-key"})
    assert outputs.status_code == 200
    assert outputs.json()[0]["asset_id"] == asset_id
    assert outputs.json()[0]["approval_status"] == "review_required"

    asset = client.get(f"/api/assets/{asset_id}", headers={"X-Internal-API-Key": "test-internal-key"})
    assert asset.status_code == 200
    assert asset.json()["source_type"] == "generated"

    video_project = client.post(
        "/api/creative/video-projects",
        json={"creative_project_id": project["id"], "format": "9:16", "target_channel": "instagram"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert video_project.status_code == 201
    video = video_project.json()

    scene = client.post(
        f"/api/creative/video-projects/{video['id']}/scenes",
        json={"video_project_id": video["id"], "scene_order": 0, "scene_type": "intro", "prompt_text": "opening", "duration_seconds": 3.5},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert scene.status_code == 201
    assert scene.json()["scene_order"] == 0

    scenes = client.get(f"/api/creative/video-projects/{video['id']}/scenes", headers={"X-Internal-API-Key": "test-internal-key"})
    assert scenes.status_code == 200
    assert len(scenes.json()) == 1


def test_creative_campaign_and_job_update(client):
    project = client.post("/api/creative/projects", json={"name": "Campaign Project", "project_type": "campaign"}, headers={"X-Internal-API-Key": "test-internal-key"}).json()
    campaign = client.post(
        "/api/creative/campaigns",
        json={"name": "Launch", "target_audience": "clubwear", "creative_project_id": project["id"]},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert campaign.status_code == 201

    updated = client.patch(
        f"/api/creative/campaigns/{campaign.json()['id']}",
        json={"status": "review_required", "target_audience": "updated audience"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "review_required"
    assert updated.json()["target_audience"] == "updated audience"

    job = client.post("/api/creative/jobs", json={"project_id": project["id"], "job_type": "prompt_generation"}, headers={"X-Internal-API-Key": "test-internal-key"}).json()
    patched_job = client.patch(
        f"/api/creative/jobs/{job['id']}",
        json={"status": "generated", "progress_percent": 100, "response_payload": {"text": "generated"}},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert patched_job.status_code == 200
    assert patched_job.json()["status"] == "generated"
    assert patched_job.json()["response_payload"]["text"] == "generated"


def test_creative_image_generation_rejects_non_image_job(client):
    project = client.post("/api/creative/projects", json={"name": "Wrong Job", "project_type": "photoshoot"}, headers={"X-Internal-API-Key": "test-internal-key"}).json()
    job = client.post("/api/creative/jobs", json={"project_id": project["id"], "job_type": "prompt_generation"}, headers={"X-Internal-API-Key": "test-internal-key"}).json()
    response = client.post(
        f"/api/creative/jobs/{job['id']}/generate-image",
        json={"prompt": "make image"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert response.status_code == 400
