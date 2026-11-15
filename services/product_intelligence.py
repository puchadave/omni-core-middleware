"""Product intelligence and channel readiness for Omni-Core."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from database import channel_readiness_get, channel_readiness_list, channel_readiness_upsert, fetch_product, product_quality_get, product_quality_upsert, asset_list

logger = logging.getLogger("omni.intelligence")

SUPPORTED_CHANNELS = ("shopware", "ebay", "amazon", "kaufland", "otto", "etsy", "google_merchant_center")


def _parse_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return default


def _text(value: Any) -> str:
    return str(value or "").strip()


def _score(parts: list[tuple[bool, float]]) -> float:
    earned = sum(weight for passed, weight in parts if passed)
    possible = sum(weight for _, weight in parts)
    if possible == 0:
        return 0.0
    return round(earned / possible * 100, 2)


async def evaluate_product_quality(sku: str) -> dict[str, Any]:
    product = await fetch_product(sku)
    if product is None:
        raise ValueError("Product not found.")

    assets = await asset_list(product_sku=sku, limit=100)
    tags = _parse_json(product.get("ai_tags_json"), [])
    title = _text(product.get("title"))
    category = _text(product.get("category"))
    raw_description = _text(product.get("raw_description"))
    ai_title = _text(product.get("ai_title"))
    ai_description = _text(product.get("ai_description_html"))
    price = float(product.get("supplier_price") or 0)
    stock = int(product.get("stock") or 0)
    status = _text(product.get("status"))
    source_hash = _text(product.get("source_hash"))

    attribute_score = _score([
        (bool(title), 34),
        (bool(category), 33),
        (price > 0, 33),
    ])
    description_score = _score([
        (len(raw_description) >= 50, 50),
        (len(ai_description) >= 50, 50),
    ])
    # The current product model has no separate variant table. Base product
    # identity is therefore scored here; variant-specific gaps are reported.
    variant_score = _score([
        (bool(title), 50),
        (bool(category), 50),
    ])
    media_score = round(min(len(assets), 3) / 3 * 100, 2)
    seo_score = _score([
        (bool(ai_title), 34),
        (bool(ai_description), 33),
        (isinstance(tags, list) and len(tags) > 0, 33),
    ])
    channel_results = await evaluate_channel_readiness(sku)
    platform_score = round(sum(item["score"] for item in channel_results) / len(channel_results), 2) if channel_results else 0.0
    technical_score = _score([
        (status == "active", 25),
        (bool(source_hash), 25),
        (price >= 0, 25),
        (stock >= 0, 25),
    ])

    total_score = round(
        attribute_score * 0.20
        + description_score * 0.20
        + variant_score * 0.10
        + media_score * 0.15
        + seo_score * 0.20
        + platform_score * 0.10
        + technical_score * 0.05,
        2,
    )

    issues: list[str] = []
    suggestions: list[str] = []
    if not title:
        issues.append("title is missing")
        suggestions.append("Add a product title.")
    if not category:
        issues.append("category is missing")
        suggestions.append("Assign a product category.")
    if price <= 0:
        issues.append("supplier price is not greater than zero")
        suggestions.append("Set a valid supplier price.")
    if len(raw_description) < 50:
        issues.append("raw description is too short")
        suggestions.append("Add a more complete raw product description.")
    if len(ai_description) < 50:
        issues.append("AI description is too short or missing")
        suggestions.append("Generate or enter an AI description.")
    if not assets:
        issues.append("no product media is assigned")
        suggestions.append("Assign at least one product image.")
    if not ai_title:
        issues.append("AI title is missing")
        suggestions.append("Generate an AI title.")
    if not (isinstance(tags, list) and tags):
        issues.append("AI tags are missing")
        suggestions.append("Generate product tags.")
    issues.append("Variant data is not modeled separately in the current product schema; only base product fields were evaluated.")
    if status != "active":
        issues.append(f"product status is {status}")
        suggestions.append("Set the product to active before publication.")
    if not source_hash:
        issues.append("source hash is missing")
        suggestions.append("Re-import or update the product source hash.")

    await product_quality_upsert(
        product_sku=sku,
        attribute_score=attribute_score,
        description_score=description_score,
        variant_score=variant_score,
        media_score=media_score,
        seo_score=seo_score,
        platform_score=platform_score,
        technical_score=technical_score,
        total_score=total_score,
        issues=issues,
        suggestions=suggestions,
    )
    return {
        "product_sku": sku,
        "attribute_score": attribute_score,
        "description_score": description_score,
        "variant_score": variant_score,
        "media_score": media_score,
        "seo_score": seo_score,
        "platform_score": platform_score,
        "technical_score": technical_score,
        "total_score": total_score,
        "issues": issues,
        "suggestions": suggestions,
    }


async def evaluate_channel_readiness(sku: str, channel: str | None = None) -> list[dict[str, Any]] | dict[str, Any]:
    product = await fetch_product(sku)
    if product is None:
        raise ValueError("Product not found.")

    assets = await asset_list(product_sku=sku, limit=100)
    title = _text(product.get("title"))
    category = _text(product.get("category"))
    raw_description = _text(product.get("raw_description"))
    ai_description = _text(product.get("ai_description_html"))
    description = ai_description or raw_description
    price = float(product.get("supplier_price") or 0)
    stock = int(product.get("stock") or 0)
    channels = [channel] if channel else list(SUPPORTED_CHANNELS)
    results = []
    for target in channels:
        if target not in SUPPORTED_CHANNELS:
            raise ValueError(f"Unsupported channel: {target}")

        checks: list[tuple[str, bool, str]] = []
        warnings: list[str] = []
        if target == "shopware":
            checks.extend([
                ("title", bool(title), "Title is required."),
                ("category", bool(category), "Category is required."),
                ("description", bool(description), "Description is required."),
                ("price", price > 0, "Price must be greater than zero."),
                ("stock", stock >= 0, "Stock cannot be negative."),
                ("media", len(assets) > 0, "At least one image is required."),
            ])
        elif target == "ebay":
            checks.extend([
                ("title", bool(title), "Title is required."),
                ("title_length", len(title) <= 80, "eBay title must be at most 80 characters."),
                ("description", bool(description), "Description is required."),
                ("price", price > 0, "Price must be greater than zero."),
                ("stock", stock > 0, "Stock must be greater than zero."),
                ("media", len(assets) > 0, "At least one image is required."),
            ])
        elif target == "amazon":
            checks.extend([
                ("title", bool(title), "Title is required."),
                ("title_length", len(title) <= 200, "Amazon title must be at most 200 characters."),
                ("category", bool(category), "Category is required."),
                ("description", bool(description), "Description is required."),
                ("price", price > 0, "Price must be greater than zero."),
                ("stock", stock > 0, "Stock must be greater than zero."),
                ("media", len(assets) > 0, "At least one image is required."),
            ])
            warnings.append("GTIN/EAN is not stored in the current product model and could not be validated.")
        elif target == "kaufland":
            checks.extend([
                ("title", bool(title), "Title is required."),
                ("category", bool(category), "Category is required."),
                ("description", bool(description), "Description is required."),
                ("price", price > 0, "Price must be greater than zero."),
                ("stock", stock > 0, "Stock must be greater than zero."),
                ("media", len(assets) > 0, "At least one image is required."),
            ])
        elif target == "otto":
            checks.extend([
                ("title", bool(title), "Title is required."),
                ("description", bool(description), "Description is required."),
                ("price", price > 0, "Price must be greater than zero."),
                ("stock", stock > 0, "Stock must be greater than zero."),
                ("media", len(assets) > 0, "At least one image is required."),
            ])
            warnings.append("OTTO category mapping is not validated because category mappings are not configured.")
        elif target == "etsy":
            checks.extend([
                ("title", bool(title), "Title is required."),
                ("title_length", len(title) <= 140, "Etsy title must be at most 140 characters."),
                ("description", bool(description), "Description is required."),
                ("price", price > 0, "Price must be greater than zero."),
                ("stock", stock > 0, "Stock must be greater than zero."),
                ("media", len(assets) > 0, "At least one image is required."),
            ])
        elif target == "google_merchant_center":
            checks.extend([
                ("title", bool(title), "Title is required."),
                ("description", bool(description), "Description is required."),
                ("price", price > 0, "Price must be greater than zero."),
                ("availability", stock >= 0, "Availability must be known."),
                ("media", len(assets) > 0, "At least one image is required."),
            ])
            warnings.append("GTIN/EAN is not stored in the current product model and could not be validated.")

        passed = sum(1 for _, ok, _ in checks if ok)
        score = round(passed / len(checks) * 100, 2) if checks else 0.0
        errors = [message for _, ok, message in checks if not ok]
        record_id = str(uuid.uuid4())
        await channel_readiness_upsert(
            record_id=record_id,
            product_sku=sku,
            channel=target,
            score=score,
            errors=errors,
            warnings=warnings,
        )
        results.append({
            "product_sku": sku,
            "channel": target,
            "score": score,
            "errors": errors,
            "warnings": warnings,
            "checked_at": None,
        })

    if channel is not None:
        return results[0]
    return results


async def get_product_quality(sku: str) -> dict[str, Any] | None:
    record = await product_quality_get(sku)
    if record is None:
        return None
    result = dict(record)
    result["issues"] = _parse_json(result.pop("issues_json", None), [])
    result["suggestions"] = _parse_json(result.pop("suggestions_json", None), [])
    return result


async def get_channel_readiness(sku: str, channel: str | None = None) -> list[dict[str, Any]] | dict[str, Any] | None:
    if channel is None:
        records = await channel_readiness_list(sku)
        parsed = []
        for record in records:
            item = dict(record)
            item["errors"] = _parse_json(item.pop("errors_json", None), [])
            item["warnings"] = _parse_json(item.pop("warnings_json", None), [])
            parsed.append(item)
        return parsed
    record = await channel_readiness_get(sku, channel)
    if record is None:
        return None
    result = dict(record)
    result["errors"] = _parse_json(result.pop("errors_json", None), [])
    result["warnings"] = _parse_json(result.pop("warnings_json", None), [])
    return result
