import pytest
from fastapi.testclient import TestClient

from main import app, runtime


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    import asyncio
    import database

    database.DATABASE_URL = ""
    database.DATABASE_PATH = tmp_path / "test.db"
    asyncio.run(database.init_db())
    return TestClient(app)


def _create_video_project(client):
    creative = client.post("/api/creative/projects", json={"name": "Video Project", "project_type": "video"}, headers={"X-Internal-API-Key": "test-internal-key"}).json()
    video = client.post("/api/creative/video-projects", json={"creative_project_id": creative["id"], "format": "9:16", "target_channel": "instagram"}, headers={"X-Internal-API-Key": "test-internal-key"})
    assert video.status_code == 201
    return video.json()


def test_video_storyboard_generates_scenes(client, monkeypatch):
    video = _create_video_project(client)

    class FakeOmniRoute:
        async def get_ai_trend_analysis(self, prompt):
            assert "video concept" in prompt
            return {"scenes": [{"scene_type": "intro", "prompt_text": "opening", "duration_seconds": 3.5, "camera_notes": "wide"}]}

    monkeypatch.setattr(runtime, "omniroute", FakeOmniRoute())
    response = client.post(
        f"/api/creative/video-projects/{video['id']}/storyboard",
        json={"prompt": "fashion reel", "scene_count": 1},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert response.status_code == 200
    assert response.json()[0]["scene_type"] == "intro"
    assert response.json()[0]["duration_seconds"] == 3.5
    assert "Camera: wide" in response.json()[0]["prompt_text"]

    scenes = client.get(f"/api/creative/video-projects/{video['id']}/scenes", headers={"X-Internal-API-Key": "test-internal-key"})
    assert scenes.status_code == 200
    assert len(scenes.json()) == 1


def test_video_storyboard_unavailable_without_omniroute(client, monkeypatch):
    video = _create_video_project(client)
    monkeypatch.setattr(runtime, "omniroute", None)

    response = client.post(
        f"/api/creative/video-projects/{video['id']}/storyboard",
        json={"prompt": "fashion reel"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert response.status_code == 400
    assert "OmniRoute is not configured" in response.json()["detail"]


def test_video_storyboard_rejects_invalid_storyboard(client, monkeypatch):
    video = _create_video_project(client)

    class FakeOmniRoute:
        async def get_ai_trend_analysis(self, prompt):
            return {"scenes": "not-a-list"}

    monkeypatch.setattr(runtime, "omniroute", FakeOmniRoute())
    response = client.post(
        f"/api/creative/video-projects/{video['id']}/storyboard",
        json={"prompt": "fashion reel"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert response.status_code == 400
