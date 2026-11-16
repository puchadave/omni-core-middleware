from __future__ import annotations

import pytest

from services.gtin_intelligence import (
    gtin_type,
    is_valid_gtin,
    lookup_gtin,
    normalize_gtin,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("4006381333931", "4006381333931"),
        (" 4006-3813-33931 ", "4006381333931"),
        ("abc", ""),
        (None, ""),
        ("", ""),
    ],
)
def test_normalize_gtin(raw, expected):
    assert normalize_gtin(raw) == expected


@pytest.mark.parametrize(
    "value,valid,reason",
    [
        ("4006381333931", True, "ok"),  # valid EAN-13
        ("036000291452", True, "ok"),   # valid UPC-A/GTIN-12
        ("96385074", True, "ok"),       # valid GTIN-8
        ("4006381333932", False, "invalid_check_digit"),
        ("123", False, "unsupported_length_3"),
        ("abcdefghijklm", False, "empty_or_non_numeric"),
    ],
)
def test_is_valid_gtin(value, valid, reason):
    ok, why = is_valid_gtin(value)
    assert ok is valid
    assert why == reason


def test_gtin_type():
    assert gtin_type("96385074") == "GTIN-8/EAN-8"
    assert gtin_type("036000291452") == "GTIN-12/UPC-A"
    assert gtin_type("4006381333931") == "GTIN-13/EAN-13"
    assert gtin_type("10012345678902") == "GTIN-14"
    assert gtin_type("123") == "unknown"


@pytest.mark.asyncio
async def test_lookup_gtin_invalid_input():
    result = await lookup_gtin("")
    assert result["valid"] is False
    assert result["reconciliation"]["status"] == "invalid_input"
    assert result["providers"] == []


@pytest.mark.asyncio
async def test_lookup_gtin_no_providers_configured(monkeypatch):
    async def fake_list_providers(enabled_only=True):
        return []

    async def fake_save_lookup(gtin, record):
        return "lookup-1"

    monkeypatch.setattr("services.gtin_intelligence.gtin_list_providers", fake_list_providers)
    monkeypatch.setattr("services.gtin_intelligence.gtin_save_lookup", fake_save_lookup)
    result = await lookup_gtin("4006381333931")
    assert result["valid"] is True
    assert result["gtin"] == "4006381333931"
    assert result["type"] == "GTIN-13/EAN-13"
    assert result["reconciliation"]["status"] == "no_providers_configured"
    assert len(result["providers"]) >= 1
    assert all(p["found"] is False for p in result["providers"])
    assert result["confidence"] == 0.4


@pytest.mark.asyncio
async def test_lookup_gtin_unknown_provider(monkeypatch):
    async def fake_list_providers(enabled_only=True):
        return [{"name": "mystery_provider", "enabled": True, "config": {}}]

    async def fake_save_lookup(gtin, record):
        return "lookup-2"

    monkeypatch.setattr("services.gtin_intelligence.gtin_list_providers", fake_list_providers)
    monkeypatch.setattr("services.gtin_intelligence.gtin_save_lookup", fake_save_lookup)
    result = await lookup_gtin("4006381333931")
    assert result["reconciliation"]["status"] == "not_found_or_not_configured"
    assert result["providers"][0]["provider"] == "mystery_provider"
    assert result["providers"][0]["error"] == "unknown_provider"


@pytest.mark.asyncio
async def test_lookup_gtin_conflict_detection(monkeypatch):
    # Simulate two providers returning conflicting chosen data via registry patching
    from services import gtin_intelligence as gi

    class P1(gi.BaseProviderAdapter):
        name = "p1"
        async def lookup(self, gtin):
            return gi.ProviderResult(provider="p1", found=True, data={"title": "Titel A"}, confidence=0.9, trust_level="A")

    class P2(gi.BaseProviderAdapter):
        name = "p2"
        async def lookup(self, gtin):
            return gi.ProviderResult(provider="p2", found=True, data={"title": "Titel B"}, confidence=0.8, trust_level="B")

    async def fake_list_providers(enabled_only=True):
        return [{"name": "p1", "enabled": True}, {"name": "p2", "enabled": True}]

    async def fake_save_lookup(gtin, record):
        return "lookup-c"

    monkeypatch.setattr(gi, "gtin_list_providers", fake_list_providers)
    monkeypatch.setattr(gi, "gtin_save_lookup", fake_save_lookup)
    monkeypatch.setitem(gi.PROVIDER_REGISTRY, "p1", P1)
    monkeypatch.setitem(gi.PROVIDER_REGISTRY, "p2", P2)
    try:
        result = await gi.lookup_gtin("4006381333931")
        assert result["reconciliation"]["status"] == "matched_with_conflicts"
        assert result["reconciliation"]["chosen_provider"] == "p1"
        assert result["reconciliation"]["chosen"]["title"] == "Titel A"
        assert any(c["field"] == "title" and c["other_value"] == "Titel B" for c in result["reconciliation"]["conflicts"])
    finally:
        gi.PROVIDER_REGISTRY.pop("p1", None)
        gi.PROVIDER_REGISTRY.pop("p2", None)


@pytest.mark.asyncio
async def test_enrich_product_by_gtin_records_and_applies_safe(monkeypatch):
    from services import gtin_intelligence as gi

    async def fake_fetch_product(sku):
        return {"sku": sku, "title": "Prod", "ean": "", "brand": ""}

    async def fake_lookup(gtin, providers=None):
        return {
            "gtin": "4006381333931", "valid": True, "confidence": 0.9, "trust_level": "A",
            "reconciliation": {"status": "matched", "chosen_provider": "p1", "conflicts": [], "chosen": {"brand": "UPTEMPO"}},
            "provenance": [],
        }

    saved_enrichments = []
    async def fake_save_enrichment(**kw):
        saved_enrichments.append(kw)
        return f"e{len(saved_enrichments)}"

    async def fake_save_conflict(**kw):
        return "c1"

    applied_payloads = []
    async def fake_upsert(data, sync_rules=None):
        applied_payloads.append((data, sync_rules))
        return {"status": "ok"}

    monkeypatch.setattr(gi, "fetch_product", fake_fetch_product)
    monkeypatch.setattr(gi, "lookup_gtin", fake_lookup)
    monkeypatch.setattr(gi, "gtin_save_enrichment", fake_save_enrichment)
    monkeypatch.setattr(gi, "gtin_save_conflict", fake_save_conflict)
    monkeypatch.setattr(gi, "upsert_product_aggregate", fake_upsert)

    res = await gi.enrich_product_by_gtin("SKU-1", "4006381333931", auto_apply_safe=True)
    assert res["ok"] is True
    assert res["applied"].get("ean") == "4006381333931"
    fields = {e["field"] for e in res["enrichments"]}
    assert "ean" in fields and "brand" in fields
    assert any(p[0].get("sku") == "SKU-1" and p[0].get("ean") == "4006381333931" for p in applied_payloads)
    assert any(e["field_name"] == "ean" and e["verification_state"] == "adopted" for e in saved_enrichments)


@pytest.mark.asyncio
async def test_enrich_product_by_gtin_records_conflict_no_overwrite(monkeypatch):
    from services import gtin_intelligence as gi

    async def fake_fetch_product(sku):
        return {"sku": sku, "title": "Prod", "ean": "4006381333931", "brand": "EXISTING"}

    async def fake_lookup(gtin, providers=None):
        return {
            "gtin": "4006381333931", "valid": True, "confidence": 0.9, "trust_level": "A",
            "reconciliation": {"status": "matched", "chosen_provider": "p1", "conflicts": [], "chosen": {"brand": "NEWBRAND"}},
            "provenance": [],
        }

    saved_enrichments = []
    async def fake_save_enrichment(**kw):
        saved_enrichments.append(kw)
        return "e1"

    saved_conflicts = []
    async def fake_save_conflict(**kw):
        saved_conflicts.append(kw)
        return f"c{len(saved_conflicts)}"

    async def fake_upsert(data, sync_rules=None):
        raise AssertionError("must not overwrite on conflict path")

    monkeypatch.setattr(gi, "fetch_product", fake_fetch_product)
    monkeypatch.setattr(gi, "lookup_gtin", fake_lookup)
    monkeypatch.setattr(gi, "gtin_save_enrichment", fake_save_enrichment)
    monkeypatch.setattr(gi, "gtin_save_conflict", fake_save_conflict)
    monkeypatch.setattr(gi, "upsert_product_aggregate", fake_upsert)

    res = await gi.enrich_product_by_gtin("SKU-1", auto_apply_safe=True)
    assert res["ok"] is True
    assert res["applied"] == {}
    assert any(c["field_name"] == "brand" and c["status"] == "open" for c in saved_conflicts)
    assert not any(e.get("field_name") == "brand" for e in saved_enrichments)
