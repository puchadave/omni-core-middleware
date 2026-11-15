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


def test_bundle_generation_creates_bundle_and_metadata(client, monkeypatch):
    for sku, category in (("SKU-1", "Hoodie"), ("SKU-2", "Pants"), ("SKU-3", "Jacket")):
        response = client.post(
            "/api/products",
            json={"sku": sku, "title": f"Test {sku}", "category": category, "supplier_price": 10.0 + len(sku), "stock": 5, "raw_description": "A" * 60},
            headers={"X-Internal-API-Key": "test-internal-key"},
        )
        assert response.status_code == 201

    class FakeOmniRoute:
        async def get_ai_trend_analysis(self, prompt):
            return {"description": "Generated bundle description."}

    monkeypatch.setattr(runtime, "omniroute", FakeOmniRoute())
    response = client.post(
        "/api/bundles/intelligence/generate",
        json={"max_bundles": 1, "min_size": 3, "max_size": 3},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert response.status_code == 200
    bundles = response.json()
    assert len(bundles) == 1
    bundle = bundles[0]
    assert len(bundle["component_skus"]) == 3
    assert bundle["quality_score"] > 0
    assert bundle["suggested_price"] > bundle["total_supplier_price"]
    assert bundle["generated_description"] == "Generated bundle description."
    assert bundle["approval_status"] == "review_required"

    detail = client.get(f"/api/bundles/intelligence/{bundle['bundle_sku']}", headers={"X-Internal-API-Key": "test-internal-key"})
    assert detail.status_code == 200
    assert len(detail.json()["products"]) == 3

    listed = client.get("/api/bundles/intelligence", headers={"X-Internal-API-Key": "test-internal-key"})
    assert listed.status_code == 200
    assert listed.json()[0]["bundle_sku"] == bundle["bundle_sku"]

    product = client.get(f"/api/products/{bundle['bundle_sku']}", headers={"X-Internal-API-Key": "test-internal-key"})
    assert product.status_code == 200
    assert bool(product.json()["is_bundle"]) is True


def test_bundle_generation_requires_enough_products(client):
    client.post("/api/products", json={"sku": "SKU-1", "title": "Only Product", "category": "Hoodie", "supplier_price": 10.0, "stock": 5}, headers={"X-Internal-API-Key": "test-internal-key"})
    response = client.post(
        "/api/bundles/intelligence/generate",
        json={"max_bundles": 1, "min_size": 3, "max_size": 3},
        headers={"X-Internal-API-Key": "test-internal-key"},
    )
    assert response.status_code == 200
    assert response.json() == []
