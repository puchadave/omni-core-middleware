"""Matterhorn product importer with -10% safety stock buffer."""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional

import httpx
import xml.etree.ElementTree as ET

from database import upsert_product, refresh_all_bundle_stocks, write_log, start_import_run, finish_import_run, get_setting

logger = logging.getLogger(__name__)

MATTERHORN_REST_API: str = "https://matterhorn-moda.de"
MATTERHORN_XML_FEED: str = "https://matterhorn-moda.de/?str=xmloffer"
STOCK_BUFFER_PERCENT: float = 0.10  # -10% safety stock buffer


class MatterhornUniversalImporter:
    """Universal importer for Matterhorn wholesale product data."""

    def __init__(self, api_token: Optional[str] = None, client: Optional[httpx.AsyncClient] = None) -> None:
        self.api_token: Optional[str] = api_token
        self.client: httpx.AsyncClient = client or httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()

    async def _get_api_token(self) -> Optional[str]:
        """Read API token from database settings table."""
        return await get_setting("matterhorn_api_token")

    async def fetch_via_rest_api(self, endpoint: str = "/api/v1/products", params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Fetch product data from Matterhorn REST API."""
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
            if isinstance(products, list):
                return products
            return []
        except httpx.HTTPStatusError as exc:
            logger.error("Matterhorn REST API error %s: %s", exc.response.status_code, exc)
            return []
        except Exception as exc:
            logger.exception("Unexpected error fetching from Matterhorn REST API: %s", exc)
            return []

    async def fetch_via_xml_offer(self) -> List[Dict[str, Any]]:
        """Parse the XML Offer Feed from Matterhorn."""
        url = MATTERHORN_XML_FEED
        try:
            response = await self.client.get(url, timeout=30.0)
            response.raise_for_status()
            root = ET.fromstring(response.text)
            products: List[Dict[str, Any]] = []
            for item in root.findall(".//product"):
                sku = item.findtext("sku") or item.findtext("SKU") or ""
                title = item.findtext("title") or item.findtext("name") or ""
                price = item.findtext("price") or item.findtext("EKP") or "0"
                category = item.findtext("category") or item.findtext("CAT") or ""
                stock = item.findtext("stock") or item.findtext("STOCK") or "0"
                products.append(
                    {
                        "sku": sku,
                        "title": title,
                        "price": float(price),
                        "category": category,
                        "stock": int(float(stock)),
                    }
                )
            return products
        except Exception as exc:
            logger.exception("Error parsing Matterhorn XML offer feed: %s", exc)
            return []

    async def import_products(self, source: str = "matterhorn_xml") -> Dict[str, Any]:
        """Run a full import: fetch, apply buffer, persist with idempotent upserts."""
        run_id = await start_import_run(source)
        imported = 0
        updated = 0
        errors = 0
        try:
            products = await self.fetch_via_xml_offer()
            if not products:
                products = await self.fetch_via_rest_api()
            for product in products:
                try:
                    raw_stock = int(product.get("stock", 0))
                    buffered_stock = max(0, int(raw_stock * (1 - STOCK_BUFFER_PERCENT)))
                    source_hash = hashlib.sha256(
                        f"{product.get('sku', '')}|{product.get('title', '')}|{product.get('price', '')}|{raw_stock}".encode()
                    ).hexdigest()
                    changed = await upsert_product(
                        sku=str(product.get("sku", "")).strip(),
                        title=str(product.get("title", "")),
                        category=str(product.get("category", "")),
                        supplier_price=float(product.get("price", 0)),
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
            await write_log("INFO", "import_complete", f"Matterhorn import finished: {imported} products, {updated} changed, {errors} errors")
            await finish_import_run(run_id, imported=imported, updated=updated, errors=errors, error_message="")
            return {"imported": imported, "updated": updated, "errors": errors, "status": "success" if errors == 0 else "failed"}
        except Exception as exc:
            logger.exception("Matterhorn import run failed: %s", exc)
            await write_log("ERROR", "import_failed", f"Matterhorn import failed: {exc}")
            await finish_import_run(run_id, imported=imported, updated=updated, errors=errors, error_message=str(exc))
            return {"imported": imported, "updated": updated, "errors": errors, "status": "failed", "error": str(exc)}


    async def preview_products(self, limit: int = 50) -> Dict[str, Any]:
        """Fetch and normalize sample products without persisting (validation/preview)."""
        try:
            products = await self.fetch_via_xml_offer()
            source = "xml"
            if not products:
                products = await self.fetch_via_rest_api()
                source = "rest"
            normalized = []
            errors = 0
            for p in products[: max(1, min(limit, 200))]:
                try:
                    raw_stock = int(p.get("stock", 0) or 0)
                    buffered_stock = max(0, int(raw_stock * (1 - STOCK_BUFFER_PERCENT)))
                    normalized.append({
                        "sku": str(p.get("sku", "")).strip(),
                        "title": str(p.get("title", "")),
                        "category": str(p.get("category", "")),
                        "supplier_price": float(p.get("price", 0) or 0),
                        "raw_stock": raw_stock,
                        "buffered_stock": buffered_stock,
                        "source": source,
                        "validation": "ok" if str(p.get("sku", "")).strip() and str(p.get("title", "")) else "warning",
                    })
                except Exception:
                    errors += 1
            return {
                "status": "ok",
                "source": source,
                "total_fetched": len(products),
                "preview_count": len(normalized),
                "errors": errors,
                "items": normalized,
                "pipeline": ["RAW", "VALIDATION", "NORMALIZATION", "PREVIEW"],
            }
        except Exception as exc:
            return {"status": "error", "error": str(exc), "items": []}
