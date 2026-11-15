"""Bundle intelligence for Omni-Core."""

from __future__ import annotations

import itertools
import logging
import uuid
from typing import Any

from database import (
    asset_list,
    bundle_intelligence_get,
    bundle_intelligence_list,
    bundle_intelligence_products,
    bundle_intelligence_replace_products,
    bundle_intelligence_update,
    bundle_intelligence_upsert,
    list_products,
    model_profile_list,
    upsert_bundle,
    write_log,
)
from services.pricing_engine import PricingEngine

logger = logging.getLogger("omni.bundles")

pricing_engine = PricingEngine()


def _product_category(product: dict[str, Any]) -> str:
    category = str(product.get("category") or "").strip()
    return category or "uncategorized"


def _product_stock(product: dict[str, Any]) -> int:
    try:
        return int(product.get("stock") or 0)
    except (TypeError, ValueError):
        return 0


def _product_price(product: dict[str, Any]) -> float:
    try:
        return float(product.get("supplier_price") or 0)
    except (TypeError, ValueError):
        return 0.0


def _candidate_quality(products: list[dict[str, Any]], has_media: list[bool]) -> float:
    categories = {_product_category(product) for product in products}
    category_score = len(categories) / len(products) * 40 if products else 0
    stock_score = 20 if all(_product_stock(product) > 0 for product in products) else 0
    prices = [_product_price(product) for product in products]
    price_score = 20 if prices and (max(prices) / max(min(prices), 0.01)) <= 4 else 10
    media_score = 20 if has_media and all(has_media) else 10 if any(has_media) else 0
    return round(category_score + stock_score + price_score + media_score, 2)


async def _generated_description(products: list[dict[str, Any]], runtime: Any | None) -> str:
    omni_route = getattr(runtime, "omniroute", None) if runtime is not None else None
    if omni_route is not None:
        prompt = (
            "Return JSON with a 'description' field. Create a concise product bundle description "
            f"for these products: {', '.join(str(product.get('title') or product.get('sku')) for product in products)}."
        )
        try:
            result = await omni_route.get_ai_trend_analysis(prompt)
            if isinstance(result, dict) and str(result.get("description") or "").strip():
                return str(result["description"]).strip()[:4000]
        except Exception as exc:
            logger.warning("Bundle description generation failed: %s", exc)
    titles = ", ".join(str(product.get("title") or product.get("sku")) for product in products)
    return f"Bundle of {titles}."


async def generate_bundles(*, max_bundles: int = 10, min_size: int = 3, max_size: int = 6,
                           created_by: str = "system", runtime: Any | None = None) -> list[dict[str, Any]]:
    if max_bundles < 1 or min_size < 2 or max_size < min_size or max_size > 8:
        raise ValueError("Invalid bundle generation parameters.")

    products = [product for product in await list_products(limit=500)
                if str(product.get("status") or "") == "active" and _product_stock(product) > 0]
    if len(products) < min_size:
        return []

    categories = sorted({_product_category(product) for product in products})
    candidates: list[tuple[float, list[dict[str, Any]], list[bool]]] = []
    seen_sets: set[frozenset[str]] = set()

    for size in range(min_size, max_size + 1):
        for category_combo in itertools.combinations(categories, size):
            chosen: list[dict[str, Any]] = []
            used_skus: set[str] = set()
            has_media: list[bool] = []
            for category in category_combo:
                options = [product for product in products
                           if _product_category(product) == category and str(product.get("sku")) not in used_skus]
                if not options:
                    chosen = []
                    break
                # Prefer available stock, then lower supplier cost, then stable SKU order.
                selected = sorted(options, key=lambda item: (-_product_stock(item), _product_price(item), str(item.get("sku"))))[0]
                chosen.append(selected)
                used_skus.add(str(selected.get("sku")))
                assets = await asset_list(product_sku=str(selected.get("sku")), limit=1)
                has_media.append(len(assets) > 0)
            if len(chosen) != size:
                continue
            sku_set = frozenset(str(product.get("sku")) for product in chosen)
            if sku_set in seen_sets:
                continue
            seen_sets.add(sku_set)
            candidates.append((_candidate_quality(chosen, has_media), chosen, has_media))

    candidates.sort(key=lambda item: (-item[0], [str(product.get("sku")) for product in item[1]]))
    if not candidates:
        return []

    active_models = [profile for profile in await model_profile_list(limit=10) if bool(profile.get("active"))]
    model_profile_id = str(active_models[0]["id"]) if active_models else None
    created: list[dict[str, Any]] = []
    for quality_score, chosen, _ in candidates[:max_bundles]:
        bundle_sku = f"BUNDLE-{uuid.uuid4().hex[:12].upper()}"
        categories = [_product_category(product) for product in chosen]
        style = ", ".join(dict.fromkeys(categories))
        title = f"{style} Bundle"
        component_skus = [str(product.get("sku")) for product in chosen]
        await upsert_bundle(bundle_sku, title, component_skus)

        total_supplier_price = round(sum(_product_price(product) for product in chosen), 2)
        suggested_price = round(sum(pricing_engine.calculate_price(_product_price(product))["final_price"] for product in chosen), 2)
        estimated_margin = round((suggested_price - total_supplier_price) / suggested_price, 4) if suggested_price > 0 else 0.0
        description = await _generated_description(chosen, runtime)
        image_concept = "Editorial outfit photo preserving each product's color, cut, pattern, logos, seams, and proportions."

        await bundle_intelligence_upsert(
            bundle_sku=bundle_sku,
            style=style,
            target_audience="Clubwear customers",
            season="all-season",
            total_supplier_price=total_supplier_price,
            suggested_price=suggested_price,
            estimated_margin=estimated_margin,
            quality_score=quality_score,
            approval_status="review_required",
            image_concept=image_concept,
            model_profile_id=model_profile_id,
            generated_description=description,
            created_by=created_by,
        )
        await bundle_intelligence_replace_products(
            bundle_sku,
            [(str(product.get("sku")), index, "Selected for category complementarity, stock availability, and bundle quality.") for index, product in enumerate(chosen)],
        )
        await write_log("INFO", "bundle.generated", "Bundle generated", {"bundle_sku": bundle_sku, "component_count": len(chosen), "quality_score": quality_score})
        created.append({
            "bundle_sku": bundle_sku,
            "title": title,
            "component_skus": component_skus,
            "style": style,
            "quality_score": quality_score,
            "total_supplier_price": total_supplier_price,
            "suggested_price": suggested_price,
            "estimated_margin": estimated_margin,
            "approval_status": "review_required",
            "generated_description": description,
        })

    return created


async def list_bundles_intelligence(limit: int = 100) -> list[dict[str, Any]]:
    records = await bundle_intelligence_list(limit=limit)
    result = []
    for record in records:
        item = dict(record)
        item["products"] = await bundle_intelligence_products(str(item["bundle_sku"]))
        result.append(item)
    return result


async def get_bundle_intelligence(bundle_sku: str) -> dict[str, Any] | None:
    record = await bundle_intelligence_get(bundle_sku)
    if record is None:
        return None
    result = dict(record)
    result["products"] = await bundle_intelligence_products(bundle_sku)
    return result


async def update_bundle_intelligence(bundle_sku: str, **fields: Any) -> bool:
    return await bundle_intelligence_update(bundle_sku, **fields)
