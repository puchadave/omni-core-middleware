import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    import database
    import services.asset_manager as asset_manager

    database.DATABASE_URL = ""
    database.DATABASE_PATH = Path(tmp_path / "test.db")

    asset_root = tmp_path / "assets"
    asset_manager.ASSET_BASE_DIR = asset_root
    asset_manager.ORIGINAL_DIR = asset_root / "original"
    asset_manager.THUMBNAIL_DIR = asset_root / "thumbnails"
    asset_manager._ensure_dirs()
    import asyncio
    asyncio.run(database.init_db())
    return TestClient(app)


def test_asset_upload_list_update_and_version(client):
    response = client.post(
        "/api/assets",
        data={
            "asset_type": "image",
            "rights_status": "owned",
            "approval_status": "draft",
        },
        files={"file": ("test.png", io.BytesIO(b"asset-bytes"), "image/png")},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert response.status_code == 201
    asset = response.json()
    assert asset["asset_type"] == "image"
    assert asset["sha256_hash"]
    assert (Path(asset["storage_path"])).exists()

    listed = client.get("/api/assets", headers={"X-Internal-API-Key": "test-internal-key"})
    assert listed.status_code == 200
    assert any(item["id"] == asset["id"] for item in listed.json())

    detail = client.get(f"/api/assets/{asset['id']}", headers={"X-Internal-API-Key": "test-internal-key"})
    assert detail.status_code == 200
    assert detail.json()["id"] == asset["id"]

    product_response = client.post(
        "/api/products",
        json={"sku": "SKU-1", "title": "Test Product", "supplier_price": 1.0, "stock": 1},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert product_response.status_code == 201

    patched = client.patch(
        f"/api/assets/{asset['id']}",
        json={"approval_status": "approved", "product_sku": "SKU-1"},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert patched.status_code == 200
    assert patched.json()["approval_status"] == "approved"
    assert patched.json()["product_sku"] == "SKU-1"

    version_response = client.post(
        f"/api/assets/{asset['id']}/versions",
        data={"change_reason": "edited"},
        files={"file": ("edited.png", io.BytesIO(b"asset-bytes-v2"), "image/png")},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert version_response.status_code == 201
    assert version_response.json()["version_number"] == 2

    versions = client.get(f"/api/assets/{asset['id']}/versions", headers={"X-Internal-API-Key": "test-internal-key"})
    assert versions.status_code == 200
    assert len(versions.json()) == 2


def test_model_profile_reference_permission_and_generation_check(client):
    profile_response = client.post(
        "/api/model-profiles",
        json={
            "display_name": "Luna",
            "model_type": "fictional",
            "consent_status": "not_required",
            "permitted_use": ["image_generation", "video_generation"],
            "rights_holder": "Omni-Core",
        },
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert profile_response.status_code == 201
    profile = profile_response.json()
    assert profile["display_name"] == "Luna"

    listed = client.get("/api/model-profiles", headers={"X-Internal-API-Key": "test-internal-key"})
    assert listed.status_code == 200
    assert any(item["id"] == profile["id"] for item in listed.json())

    detail = client.get(f"/api/model-profiles/{profile['id']}", headers={"X-Internal-API-Key": "test-internal-key"})
    assert detail.status_code == 200

    updated = client.patch(
        f"/api/model-profiles/{profile['id']}",
        json={"style_description": "Editorial fashion model", "active": True},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert updated.status_code == 200
    assert updated.json()["style_description"] == "Editorial fashion model"

    asset_response = client.post(
        "/api/assets",
        data={"asset_type": "image", "rights_status": "owned"},
        files={"file": ("reference.png", io.BytesIO(b"reference-bytes"), "image/png")},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert asset_response.status_code == 201
    asset = asset_response.json()

    reference_response = client.post(
        f"/api/model-profiles/{profile['id']}/references",
        json={"reference_type": "front", "asset_id": asset["id"], "is_primary": True},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert reference_response.status_code == 201
    assert reference_response.json()["reference_type"] == "front"

    references = client.get(f"/api/model-profiles/{profile['id']}/references", headers={"X-Internal-API-Key": "test-internal-key"})
    assert references.status_code == 200
    assert len(references.json()) == 1

    permission_response = client.post(
        f"/api/model-profiles/{profile['id']}/permissions",
        json={"permission_type": "image_generation", "granted": True},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert permission_response.status_code == 200

    permissions = client.get(f"/api/model-profiles/{profile['id']}/permissions", headers={"X-Internal-API-Key": "test-internal-key"})
    assert permissions.status_code == 200
    assert permissions.json()[0]["permission_type"] == "image_generation"

    check = client.get(
        f"/api/model-profiles/{profile['id']}/generation-check?permission_type=image_generation",
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert check.status_code == 200
    assert check.json()["allowed"] is True


def test_real_person_model_requires_consent(client):
    response = client.post(
        "/api/model-profiles",
        json={
            "display_name": "Real Person",
            "model_type": "real_person",
            "consent_status": "pending",
        },
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert response.status_code == 201

    denied = client.get(
        f"/api/model-profiles/{response.json()['id']}/generation-check?permission_type=image_generation",
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert denied.status_code == 200
    assert denied.json()["allowed"] is False
