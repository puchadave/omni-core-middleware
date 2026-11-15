"""Matterhorn product importer with preview/validation/history pipeline."""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any, Dict, List, Optional

import httpx
import xml.etree.ElementTree as ET

from database import (
    upsert_product, refresh_all_bundle_stocks, write_log, start_import_run, finish_import_run,
    get_setting, matterhorn_list_feeds, matterhorn_save_preview, matterhorn_upsert_feed,
    recent_import_runs,
)

logger = logging.getLogger(__name__)

MATTERHORN_REST_API: str = "https://matterhorn-moda.de"
MATTERHORN_XML_FEED: str = "https://matterhorn-moda.de/?str=xmloffer"
STOCK_BUFFER_PERCENT: float = 0.10


async def _publish_import_event(event_type: str, payload: dict) -> None:
    try:
        from services import event_bus as _eb_mod
        bus = getattr(_eb_mod, "event_bus", None)
        if bus is not None and getattr(bus, "_running", False):
            await bus.publish(event_type=event_type, source_module="matterhorn_importer", payload=payload, target_module="*", entity_id=str(payload.get("run_id") or payload.get("preview_id") or ""))
    except Exception:
        pass


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(str(v).replace(",", ".").strip() or default)
    except Exception:
        return default


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(str(v).replace(",", ".").strip() or default))
    except Exception:
        return default


def _extract_text(item: ET.Element, *names: str) -> str:
    for n in names:
        val = item.findtext(n)
        if val is None:
            # case-insensitive/attribute-ish fallback
            for child in list(item):
                if child.tag.lower() == n.lower() and (child.text or "").strip():
                    return (child.text or "").strip()
        if val and str(val).strip():
            return str(val).strip()
    return ""


def _validate_products(products: List[Dict[str, Any]]) -> Dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen_skus: set[str] = set()
    valid = 0
    for idx, p in enumerate(products):
        sku = str(p.get("sku") or "").strip()
        title = str(p.get("title") or "").strip()
        price = _to_float(p.get("price"), 0.0)
        stock = _to_int(p.get("stock"), 0)
        ean = str(p.get("ean") or "").strip()
        row_err = []
        if not sku:
            row_err.append("missing_sku")
        if not title:
            row_err.append("missing_title")
        if price < 0:
            row_err.append("negative_price")
        if stock < 0:
            row_err.append("negative_stock")
        if ean and (not ean.isdigit() or len(ean) not in (8, 12, 13, 14)):
            warnings.append({"index": idx, "sku": sku, "issue": "ean_format_suspicious", "value": ean})
        if sku:
            if sku in seen_skus:
                warnings.append({"index": idx, "sku": sku, "issue": "duplicate_sku_in_feed"})
            seen_skus.add(sku)
        if row_err:
            errors.append({"index": idx, "sku": sku, "issues": row_err})
        else:
            valid += 1
    status = "ok" if not errors else ("partial" if valid else "invalid")
    return {
        "status": status,
        "total": len(products),
        "valid": valid,
        "invalid": len(products) - valid,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors[:100],
        "warnings": warnings[:100],
    }


class MatterhornUniversalImporter:
    """Importer for Matterhorn product data with preview/import modes."""

    def __init__(self, api_token: Optional[str] = None, client: Optional[httpx.AsyncClient] = None) -> None:
        self.api_token: Optional[str] = api_token
        self.client: httpx.AsyncClient = client or httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self.client.aclose()

    async def _get_api_token(self) -> Optional[str]:
        return await get_setting("matterhorn_api_token")

    async def _resolve_feed_url(self, feed_url: str | None = None) -> str:
        if feed_url:
            return feed_url
        cfg_url = await get_setting("matterhorn_feed_url")
        if cfg_url:
            return cfg_url
        try:
            feeds = await matterhorn_list_feeds()
            for f in feeds:
                enabled = f.get("enabled", True)
                if isinstance(enabled, str):
                    enabled = enabled.lower() in ("1", "true", "yes")
                if enabled and f.get("feed_url"):
                    return str(f.get("feed_url"))
        except Exception:
            pass
        return MATTERHORN_XML_FEED

    async def fetch_via_rest_api(self, endpoint: str = "/api/v1/products", params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        token = self.api_token or await self._get_api_token()
        headers: Dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"{MATTERHORN_REST_API}{endpoint}"
        try:
            response = await self.client.get(url, headers=headers, params=params or {})
            response.raise_for_status()
            data = response.json()
            products = data.get("products", [])
            return products if isinstance(products, list) else []
        except httpx.HTTPStatusError as exc:
            logger.error("Matterhorn REST API error %s: %s", exc.response.status_code, exc)
            return []
        except Exception as exc:
            logger.exception("Unexpected error fetching from Matterhorn REST API: %s", exc)
            return []

    async def fetch_via_xml_offer(self, feed_url: str | None = None) -> List[Dict[str, Any]]:
        url = await self._resolve_feed_url(feed_url)
        try:
            response = await self.client.get(url, timeout=30.0)
            response.raise_for_status()
            root = ET.fromstring(response.text)
            products: List[Dict[str, Any]] = []
            # support common structures
            items = root.findall(".//product") or root.findall(".//item") or list(root)
            for item in items:
                if not list(item):
                    continue
                sku = _extract_text(item, "sku", "SKU", "artnr", "article_no", "id")
                title = _extract_text(item, "title", "name", "bezeichnung", "product_name")
                price = _extract_text(item, "price", "EKP", "ekp", "supplier_price", "netto")
                category = _extract_text(item, "category", "CAT", "kategorie")
                stock = _extract_text(item, "stock", "STOCK", "bestand", "qty", "quantity")
                ean = _extract_text(item, "ean", "EAN", "gtin", "GTIN", "barcode")
                description = _extract_text(item, "description", "desc", "beschreibung")
                image = _extract_text(item, "image", "img", "image_url", "bild", "picture")
                if not sku and not title:
                    continue
                products.append({
                    "sku": sku,
                    "title": title,
                    "price": _to_float(price, 0.0),
                    "category": category,
                    "stock": _to_int(stock, 0),
                    "ean": ean,
                    "description": description,
                    "image_url": image,
                    "source_feed": url,
                })
            return products
        except Exception as exc:
            logger.exception("Error parsing Matterhorn XML offer feed: %s", exc)
            raise

    async def fetch_products(self, feed_url: str | None = None, prefer: str = "xml") -> List[Dict[str, Any]]:
        if prefer == "rest":
            products = await self.fetch_via_rest_api()
            if products:
                return products
        try:
            products = await self.fetch_via_xml_offer(feed_url)
            if products:
                return products
        except Exception:
            if prefer == "xml":
                return []
        return await self.fetch_via_rest_api()

    async def preview_products(self, *, feed_url: str | None = None, save: bool = True, limit: int = 50) -> Dict[str, Any]:
        resolved_url = await self._resolve_feed_url(feed_url)
        try:
            products = await self.fetch_products(feed_url=resolved_url)
            validation = _validate_products(products)
            src_material = f"{resolved_url}|{len(products)}|" + "|".join([str(p.get('sku',''))+':'+str(p.get('price',''))+':'+str(p.get('stock','')) for p in products[:200]])
            source_hash = hashlib.sha256(src_material.encode()).hexdigest()
            summary = {
                "feed_url": resolved_url,
                "total": len(products),
                "valid": validation.get("valid", 0),
                "invalid": validation.get("invalid", 0),
                "sample_count": min(limit, len(products)),
            }
            preview_id = str(uuid.uuid4())
            sample = products[: max(0, min(limit, 200))]
            if save:
                await matterhorn_save_preview(preview_id=preview_id, feed_url=resolved_url, status=validation.get("status", "ok"), summary=summary, items=sample, validation=validation, source_hash=source_hash)
            await write_log("INFO", "matterhorn_preview", f"Preview generated: {len(products)} items ({validation.get('status')})", {"feed_url": resolved_url, "preview_id": preview_id})
            await _publish_import_event("matterhorn.preview_completed", {"preview_id": preview_id, "feed_url": resolved_url, "summary": summary, "validation": validation})
            return {"status": "ok", "preview_id": preview_id if save else None, "feed_url": resolved_url, "summary": summary, "validation": validation, "items": sample, "source_hash": source_hash}
        except Exception as exc:
            await write_log("ERROR", "matterhorn_preview_failed", f"Preview failed: {exc}", {"feed_url": resolved_url})
            await _publish_import_event("matterhorn.preview_failed", {"feed_url": resolved_url, "error": str(exc)})
            return {"status": "error", "feed_url": resolved_url, "error": str(exc), "summary": {}, "validation": {"status": "invalid", "errors": [{"issues": ["fetch_or_parse_failed"], "message": str(exc)}]}, "items": []}

    async def import_products(self, source: str = "matterhorn_xml", *, feed_url: str | None = None, mode: str = "safe", dry_run: bool = False, preview_id: str | None = None, confirm_full_without_preview: bool = False) -> Dict[str, Any]:
        run_id = await start_import_run(source)
        imported = 0
        updated = 0
        errors = 0
        skipped = 0
        mode = (mode or "safe").lower()
        try:
            products = await self.fetch_products(feed_url=feed_url)
            validation = _validate_products(products)
            # safety: full import without preview requires explicit confirmation or dry-run/preview flow; safe/partial modes allow
            if mode == "full" and not preview_id and not confirm_full_without_preview and not dry_run:
                await finish_import_run(run_id, imported=0, updated=0, errors=1, error_message="Full import requires preview confirmation")
                return {"status": "blocked", "reason": "preview_confirmation_required", "message": "Full-Import ohne Preview erfordert confirm_full_without_preview=true oder vorherige Preview.", "validation": validation}
            if validation.get("status") == "invalid" and mode != "dry_run":
                await finish_import_run(run_id, imported=0, updated=0, errors=validation.get("error_count", 0), error_message="Validation failed")
                return {"status": "failed", "reason": "validation_failed", "validation": validation, "imported": 0, "updated": 0, "errors": validation.get("error_count", 0)}
            if dry_run or mode == "dry_run":
                await finish_import_run(run_id, imported=0, updated=0, errors=0, error_message="dry_run")
                return {"status": "dry_run", "imported": 0, "updated": 0, "errors": 0, "skipped": 0, "validation": validation, "total": len(products), "preview_id": preview_id}

            invalid_skus = {e.get("sku") for e in (validation.get("errors") or []) if e.get("sku")}
            for product in products:
                try:
                    sku = str(product.get("sku", "")).strip()
                    if mode in ("safe", "partial") and sku in invalid_skus:
                        skipped += 1
                        continue
                    raw_stock = _to_int(product.get("stock", 0), 0)
                    buffered_stock = max(0, int(raw_stock * (1 - STOCK_BUFFER_PERCENT)))
                    source_hash = hashlib.sha256(
                        f"{sku}|{product.get('title', '')}|{product.get('price', '')}|{raw_stock}|{product.get('ean','')}".encode()
                    ).hexdigest()
                    changed = await upsert_product(
                        sku=sku,
                        title=str(product.get("title", "")),
                        category=str(product.get("category", "")),
                        supplier_price=_to_float(product.get("price", 0), 0.0),
                        stock=buffered_stock,
                        source_hash=source_hash,
                        raw_description=str(product.get("description", "")),
                    )
                    if changed:
                        updated += 1
                    imported += 1
                except Exception as exc:
                    errors += 1
                    logger.error("Error importing product %s: %s", product.get("sku", "?"), exc)
            await refresh_all_bundle_stocks()
            status = "success" if errors == 0 else "failed"
            await write_log("INFO" if errors == 0 else "WARNING", "matterhorn_import_complete", f"Matterhorn import finished: {imported} products, {updated} changed, {errors} errors, {skipped} skipped (mode={mode})", {"run_id": run_id, "mode": mode})
            await finish_import_run(run_id, imported=imported, updated=updated, errors=errors, error_message="")
            await _publish_import_event("matterhorn.import_completed", {"run_id": run_id, "imported": imported, "updated": updated, "errors": errors, "skipped": skipped, "mode": mode, "status": status})
            return {"imported": imported, "updated": updated, "errors": errors, "skipped": skipped, "status": status, "mode": mode, "validation": validation, "run_id": run_id}
        except Exception as exc:
            logger.exception("Matterhorn import run failed: %s", exc)
            await write_log("ERROR", "matterhorn_import_failed", f"Matterhorn import failed: {exc}", {"run_id": run_id})
            await finish_import_run(run_id, imported=imported, updated=updated, errors=errors, error_message=str(exc))
            await _publish_import_event("matterhorn.import_failed", {"run_id": run_id, "error": str(exc)})
            return {"imported": imported, "updated": updated, "errors": errors, "skipped": skipped, "status": "failed", "error": str(exc), "run_id": run_id}
