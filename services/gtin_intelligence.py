"""GTIN/EAN Product Intelligence: normalization, validation, provider framework, enrichment & confidence.

Design notes:
- No external provider calls are fabricated. Provider adapters are configurable and run only when
  explicitly configured with verified endpoint/credentials and enabled.
- EAN/GTIN values are never invented; provenance/confidence are tracked per field.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Optional

from database import (
    gtin_save_lookup, gtin_list_lookups, gtin_save_enrichment, gtin_list_enrichments,
    gtin_save_conflict, gtin_list_conflicts, gtin_upsert_provider, gtin_list_providers,
    fetch_product, upsert_product_aggregate,
)

logger = logging.getLogger(__name__)


def normalize_gtin(value: str | None) -> str:
    if not value:
        return ""
    s = re.sub(r"\s+", "", str(value)).replace("-", "")
    if not s.isdigit():
        return ""
    # pad common lengths to GTIN-14 for comparison when appropriate? keep normalized digits
    return s


def is_valid_gtin(value: str | None) -> tuple[bool, str]:
    s = normalize_gtin(value)
    if not s:
        return False, "empty_or_non_numeric"
    if len(s) not in (8, 12, 13, 14):
        return False, f"unsupported_length_{len(s)}"
    # check digit (mod10) - rightmost is check digit
    digits = [int(c) for c in s]
    body, check = digits[:-1], digits[-1]
    # weights from right to left on body: 3,1,3,1...
    total = 0
    weight = 3
    for d in reversed(body):
        total += d * weight
        weight = 1 if weight == 3 else 3
    calc = (10 - (total % 10)) % 10
    return (calc == check), ("ok" if calc == check else "invalid_check_digit")


def gtin_type(value: str | None) -> str:
    s = normalize_gtin(value)
    return {8: "GTIN-8/EAN-8", 12: "GTIN-12/UPC-A", 13: "GTIN-13/EAN-13", 14: "GTIN-14"}.get(len(s), "unknown")


@dataclass
class ProviderResult:
    provider: str
    found: bool
    data: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    trust_level: str = "C"  # A|B|C
    license_info: str = ""
    source_record_id: str = ""
    error: str = ""


class BaseProviderAdapter:
    name: str = "base"
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}

    async def lookup(self, gtin: str) -> ProviderResult:
        return ProviderResult(provider=self.name, found=False, error="not_configured")


class OpenEANAdapter(BaseProviderAdapter):
    name = "openean"
    async def lookup(self, gtin: str) -> ProviderResult:
        # Real implementation requires verified API docs/auth/terms/license. Do not fabricate endpoints.
        endpoint = (self.config or {}).get("endpoint") or ""
        if not endpoint or not (self.config or {}).get("enabled"):
            return ProviderResult(provider=self.name, found=False, error="not_configured_or_unverified", trust_level="C",
                                  data={"note": "OpenEAN-Adapter deaktiviert: Endpoint/Doku/Lizenz/Zugang nicht verifiziert konfiguriert."})
        # Placeholder for verified implementation (http client call would go here with rate limiting)
        return ProviderResult(provider=self.name, found=False, error="implementation_requires_verified_api_access", trust_level="C")


class GS1VerifiedAdapter(BaseProviderAdapter):
    name = "gs1_verified"
    async def lookup(self, gtin: str) -> ProviderResult:
        if not (self.config or {}).get("enabled"):
            return ProviderResult(provider=self.name, found=False, error="not_configured_requires_authorized_access", trust_level="A")
        return ProviderResult(provider=self.name, found=False, error="implementation_requires_authorized_access", trust_level="A")


PROVIDER_REGISTRY: dict[str, type[BaseProviderAdapter]] = {
    "openean": OpenEANAdapter,
    "gs1_verified": GS1VerifiedAdapter,
}


async def lookup_gtin(gtin: str, providers: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    normalized = normalize_gtin(gtin)
    valid, reason = is_valid_gtin(normalized)
    record = {
        "gtin": normalized,
        "type": gtin_type(normalized),
        "valid": valid,
        "validation": reason,
        "providers": [],
        "reconciliation": {"status": "no_external_data", "conflicts": [], "chosen": {}},
        "confidence": 0.0 if not valid else 0.4,
        "trust_level": "C",
        "provenance": [],
    }
    if not normalized:
        record["reconciliation"]["status"] = "invalid_input"
        return record

    provider_configs = providers or await gtin_list_providers(enabled_only=True)
    # if none configured, use registry placeholders as status info (no calls)
    if not provider_configs:
        for name in PROVIDER_REGISTRY:
            record["providers"].append({"provider": name, "found": False, "error": "not_configured", "note": "Provider nicht konfiguriert/aktiviert."})
        record["reconciliation"]["status"] = "no_providers_configured"
    else:
        results: list[ProviderResult] = []
        for pc in provider_configs:
            name = (pc.get("name") or pc.get("provider") or "").lower()
            cls = PROVIDER_REGISTRY.get(name)
            if not cls:
                record["providers"].append({"provider": name or "unknown", "found": False, "error": "unknown_provider"})
                continue
            adapter = cls(pc.get("config") if isinstance(pc.get("config"), dict) else pc)
            res = await adapter.lookup(normalized)
            results.append(res)
            record["providers"].append({
                "provider": res.provider, "found": res.found, "confidence": res.confidence,
                "trust_level": res.trust_level, "error": res.error, "license_info": res.license_info,
                "data_keys": list(res.data.keys()) if res.data else [],
            })
        found = [r for r in results if r.found and r.data]
        if found:
            # simple reconciliation: prefer higher trust A>B>C then confidence
            rank = {"A": 3, "B": 2, "C": 1}
            found.sort(key=lambda r: (rank.get(r.trust_level, 0), r.confidence), reverse=True)
            chosen = found[0]
            conflicts: list[dict[str, Any]] = []
            # detect conflicting values for same keys across providers
            for f in found[1:]:
                for k, v in (f.data or {}).items():
                    if k in (chosen.data or {}) and chosen.data.get(k) not in (None, "", v):
                        conflicts.append({"field": k, "chosen_provider": chosen.provider, "chosen_value": chosen.data.get(k), "other_provider": f.provider, "other_value": v})
            status = "matched_with_conflicts" if conflicts else "matched"
            record["reconciliation"] = {"status": status, "chosen_provider": chosen.provider, "conflicts": conflicts, "chosen": chosen.data}
            record["confidence"] = chosen.confidence
            record["trust_level"] = chosen.trust_level
            record["provenance"].append({"field": "*", "source_provider": chosen.provider, "source_record_id": chosen.source_record_id, "license": chosen.license_info})
        else:
            record["reconciliation"]["status"] = "not_found_or_not_configured"

    await gtin_save_lookup(normalized, record)
    return record


# Field mapping from external/provider data to canonical product fields (safe subset only)
_PROVIDER_TO_PRODUCT_FIELD = {
    "title": "title",
    "name": "title",
    "brand": "brand",
    "manufacturer": "manufacturer",
    "category": "category",
    "description": "description",
    "short_description": "short_description",
    "image_url": "image_url",
}


async def enrich_product_by_gtin(
    sku: str,
    gtin: str | None = None,
    *,
    auto_apply_safe: bool = False,
    actor: str = "system",
) -> dict[str, Any]:
    """Lookup GTIN and persist enrichment/conflict records for a canonical product.

    - Never invents GTINs; only uses provided/product GTIN.
    - Records provenance/confidence/verification state per enrichment.
    - By default does not overwrite existing non-empty fields; conflicts are recorded for review.
    - auto_apply_safe: only fills missing/empty safe fields (ean when missing) and never overwrites.
    """
    product = await fetch_product(sku)
    if not product:
        return {"ok": False, "error": "product_not_found", "sku": sku}

    source_gtin = normalize_gtin(gtin or product.get("ean") or product.get("gtin") or product.get("barcode") or "")
    if not source_gtin:
        return {"ok": False, "error": "missing_gtin", "sku": sku, "message": "Kein GTIN/EAN am Produkt vorhanden und keines übergeben."}

    lookup = await lookup_gtin(source_gtin)
    chosen = (lookup.get("reconciliation") or {}).get("chosen") or {}
    provider = (lookup.get("reconciliation") or {}).get("chosen_provider") or ""
    trust = lookup.get("trust_level") or "C"
    confidence = float(lookup.get("confidence") or 0.0)
    enrichments: list[dict[str, Any]] = []
    conflicts_out: list[dict[str, Any]] = []

    # Always ensure normalized EAN is recorded as adoptable if valid and product has none
    if lookup.get("valid") and not normalize_gtin(product.get("ean") or product.get("gtin") or ""):
        eid = await gtin_save_enrichment(
            entity_type="product", entity_id=sku, field_name="ean",
            original_value=str(product.get("ean") or ""), normalized_value=lookup["gtin"],
            source_provider=provider or "normalization", source_record_id="",
            confidence=max(confidence, 0.7 if provider else 0.5), trust_level=trust,
            verification_state="adopted" if auto_apply_safe else "proposed",
            license_info="", metadata={"reason": "normalized_valid_gtin", "actor": actor},
        )
        enrichments.append({"id": eid, "field": "ean", "value": lookup["gtin"], "state": "adopted" if auto_apply_safe else "proposed"})

    # Map provider-chosen fields conservatively
    for src_key, dst_field in _PROVIDER_TO_PRODUCT_FIELD.items():
        if src_key not in chosen:
            continue
        new_val = str(chosen.get(src_key) or "").strip()
        if not new_val:
            continue
        cur_val = str(product.get(dst_field) or "").strip()
        if not cur_val:
            state = "adopted" if auto_apply_safe else "proposed"
            eid = await gtin_save_enrichment(
                entity_type="product", entity_id=sku, field_name=dst_field,
                original_value=cur_val, normalized_value=new_val,
                source_provider=provider, source_record_id="", confidence=confidence,
                trust_level=trust, verification_state=state, license_info="",
                metadata={"lookup_gtin": lookup["gtin"], "actor": actor},
            )
            enrichments.append({"id": eid, "field": dst_field, "value": new_val, "state": state})
        elif cur_val != new_val:
            cid = await gtin_save_conflict(
                entity_type="product", entity_id=sku, field_name=dst_field,
                candidates=[
                    {"value": cur_val, "source_provider": "existing", "confidence": 1.0, "trust_level": "B"},
                    {"value": new_val, "source_provider": provider, "confidence": confidence, "trust_level": trust},
                ],
                status="open", resolution={},
            )
            conflicts_out.append({"id": cid, "field": dst_field, "existing": cur_val, "candidate": new_val})

    # Persist explicit provider conflicts from lookup reconciliation
    for c in (lookup.get("reconciliation") or {}).get("conflicts") or []:
        field_name = str(c.get("field") or "unknown")
        cid = await gtin_save_conflict(
            entity_type="product", entity_id=sku, field_name=field_name,
            candidates=[
                {"value": c.get("chosen_value"), "source_provider": c.get("chosen_provider"), "confidence": confidence, "trust_level": trust},
                {"value": c.get("other_value"), "source_provider": c.get("other_provider")},
            ],
            status="open", resolution={"source": "lookup_reconciliation"},
        )
        conflicts_out.append({"id": cid, "field": field_name, "existing": c.get("chosen_value"), "candidate": c.get("other_value"), "source": "provider_conflict"})

    applied: dict[str, Any] = {}
    if auto_apply_safe:
        # Apply only safe adopted fills (EAN missing). Never overwrite existing values.
        for e in enrichments:
            if e.get("state") == "adopted" and e.get("field") == "ean" and not normalize_gtin(product.get("ean") or ""):
                applied["ean"] = e["value"]
        if applied:
            await upsert_product_aggregate({"sku": sku, **applied}, sync_rules={"mode": "partial", "preserve_manual_seo": True, "preserve_marketplace_mappings": True})

    return {
        "ok": True,
        "sku": sku,
        "gtin": lookup.get("gtin"),
        "lookup": {"valid": lookup.get("valid"), "status": (lookup.get("reconciliation") or {}).get("status"), "confidence": lookup.get("confidence"), "trust_level": lookup.get("trust_level")},
        "enrichments": enrichments,
        "conflicts": conflicts_out,
        "applied": applied,
        "auto_apply_safe": auto_apply_safe,
    }
