import pytest
from fastapi.testclient import TestClient

from main import app


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


def test_product_quality_and_channel_readiness(client):
    response = client.post(
        "/api/products",
        json={"sku": "SKU-1", "title": "Test Product", "category": "Streetwear", "supplier_price": 10.0, "stock": 5, "raw_description": "A" * 60},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert response.status_code == 201

    # Add an image asset for media scoring.
    asset = client.post(
        "/api/assets",
        data={"asset_type": "image", "rights_status": "owned"},
        files={"file": ("image.png", __import__("io").BytesIO(b"image"), "image/png")},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert asset.status_code == 201
    client.patch(f"/api/assets/{asset.json()['id']}", json={"product_sku": "SKU-1"}, headers={"X-Internal-API-Key": "test-internal-key"})

    quality = client.get("/api/product-quality/SKU-1", headers={"X-Internal-API-Key": "test-internal-key"})
    assert quality.status_code == 200
    body = quality.json()
    assert body["total_score"] > 0
    assert body["media_score"] == 33.33
    assert any("Variant data is not modeled separately" in issue for issue in body["issues"])

    readiness = client.get("/api/channel-readiness/SKU-1", headers={"X-Internal-API-Key": "test-internal-key"})
    assert readiness.status_code == 200
    channels = {item["channel"]: item for item in readiness.json()}
    assert set(channels) == {"shopware", "ebay", "amazon", "kaufland", "otto", "etsy", "google_merchant_center"}
    assert channels["ebay"]["score"] > 0

    single = client.get("/api/channel-readiness/SKU-1/ebay", headers={"X-Internal-API-Key": "test-internal-key"})
    assert single.status_code == 200
    assert single.json()["channel"] == "ebay"


def test_product_quality_missing_product(client):
    response = client.get("/api/product-quality/MISSING", headers={"X-Internal-API-Key": "test-internal-key"})
    assert response.status_code == 404
