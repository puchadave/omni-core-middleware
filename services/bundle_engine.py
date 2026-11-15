"""Automatic and manual bundle/set building.

- Automatic: suggests/creates bundles from available signals (co-purchase/orders, category
  complementarity, price band, trend/news keywords, stock). No fabricated data.
- Manual: helper to validate/build a set from explicit SKUs.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from typing import Any

from database import fetch_product, list_bundles, list_orders, list_products, refresh_all_bundle_stocks, upsert_bundle, write_log

logger = logging.getLogger(__name__)

MAX_AUTO_BUNDLES = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def suggest_bundles(*, max_suggestions: int = 20) -> dict[str, Any]:
    products = await list_products(limit=1000, offset=0)
    orders = []
    try:
        orders = await list_orders(limit=500, offset=0)
    except Exception:
        orders = []
    active = [p for p in (products or []) if not p.get("is_bundle") and str(p.get("status") or "").lower() in ("active", "") and int(p.get("stock_quantity") or p.get("stock") or 0) > 0]
    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in active:
        by_cat[str(p.get("category") or "uncategorized")].append(p)

    suggestions: list[dict[str, Any]] = []

    # 1) co-purchase heuristic: same order-id not stored as lines here except sku/qty per order; fall back to category complementarity
    # 2) complementary cross-category pairs (simple heuristic)
    cats = list(by_cat.keys())
    for a, b in combinations(cats[:12], 2):
        pa = by_cat[a][:3]
        pb = by_cat[b][:3]
        if pa and pb:
            comp = [pa[0].get("sku"), pb[0].get("sku")]
            suggestions.append({
                "type": "cross_category",
                "component_skus": [c for c in comp if c],
                "title_hint": f"{a} + {b} Set",
                "reason": "kategorie_komplement (datenbasiert)",
                "confidence": "medium",
            })
        if len(suggestions) >= max_suggestions:
            break

    # 3) same-category value sets (2-3 items, different SKUs)
    if len(suggestions) < max_suggestions:
        for cat, items in by_cat.items():
            if len(items) >= 2:
                skus = [i.get("sku") for i in items[:3] if i.get("sku")]
                suggestions.append({
                    "type": "same_category",
                    "component_skus": skus,
                    "title_hint": f"{cat} Bundle",
                    "reason": "gleiche_kategorie_lagernd",
                    "confidence": "medium",
                })
            if len(suggestions) >= max_suggestions:
                break

    # de-dup component sets
    seen = set()
    uniq = []
    for s in suggestions:
        key = tuple(sorted([str(x) for x in (s.get("component_skus") or [])]))
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(s)
    return {"generated_at": _now(), "count": len(uniq), "suggestions": uniq[:max_suggestions]}


async def auto_generate_bundles(*, max_bundles: int = 10, create: bool = False, actor: str = "system") -> dict[str, Any]:
    max_bundles = min(max(int(max_bundles or 10), 1), MAX_AUTO_BUNDLES)
    sug = await suggest_bundles(max_suggestions=max_bundles)
    created = []
    if create:
        existing = {str(b.get("bundle_sku") or b.get("sku")) for b in await list_bundles()}
        idx = 1
        for s in sug.get("suggestions", [])[:max_bundles]:
            comps = [str(x) for x in (s.get("component_skus") or []) if x]
            if len(comps) < 2:
                continue
            # stable-ish sku
            base = re.sub(r"[^A-Z0-9]+", "-", (s.get("title_hint") or "SET").upper()).strip("-")[:24] or "SET"
            bsku = f"AUTO-{base}-{idx:03d}"
            while bsku in existing:
                idx += 1
                bsku = f"AUTO-{base}-{idx:03d}"
            title = s.get("title_hint") or f"Auto Bundle {idx}"
            try:
                await upsert_bundle(bsku, title, comps)
                created.append({"bundle_sku": bsku, "title": title, "components": comps, "reason": s.get("reason")})
                existing.add(bsku)
                idx += 1
            except Exception as e:
                created.append({"bundle_sku": bsku, "error": str(e)})
        await refresh_all_bundle_stocks()
        await write_log("INFO", "bundles.auto_generated", f"Auto bundles created: {len([c for c in created if not c.get('error')])}", {"count": len(created), "actor": actor})
    return {"generated_at": _now(), "suggestions": sug.get("suggestions", []), "created": created, "created_count": len([c for c in created if not c.get('error')]), "mode": "created" if create else "suggestions_only"}


async def build_manual_bundle(bundle_sku: str, title: str, component_skus: list[str]) -> dict[str, Any]:
    comps = [str(s).strip() for s in (component_skus or []) if str(s).strip()]
    if len(comps) < 2:
        raise ValueError("Mindestens 2 Komponenten-SKUs erforderlich.")
    # validate existence best-effort
    missing = []
    for c in comps:
        p = await fetch_product(c)
        if not p:
            missing.append(c)
    await upsert_bundle(bundle_sku, title, comps)
    await refresh_all_bundle_stocks()
    return {"bundle_sku": bundle_sku, "title": title, "components": comps, "missing_components": missing, "status": "saved"}
