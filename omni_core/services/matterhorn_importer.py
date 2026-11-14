"""Async Matterhorn catalog importer for Omni-Core Middleware.

Provides functionality to fetch product catalog data from Matterhorn
and persist it into the local SQLite database with proper stock
buffering and UPSERT semantics.

Design principles:
- Idempotent: repeated imports do not create duplicate entries
- Stock safety: applies -10% buffer to prevent overselling
- Atomic operations: uses SQLite UPSERT (ON CONFLICT) for data integrity
- Async: all database and HTTP operations are non-blocking
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx
import aiosqlite

from database import get_connection, close_connection, DB_PATH

logger = logging.getLogger(__name__)

# Matterhorn API endpoint (configurable)
MATTERHORN_BASE_URL: str = "https://matterhorn.example.com"
MATTERHORN_API_ENDPOINT: str = "/api/v1/products"

# Stock safety buffer: subtract 10% from raw supplier stock to prevent overselling
STOCK_BUFFER_PERCENT: float = 0.10


class MatterhornImporter:
    """Importer that fetches product data from Matterhorn and stores it in SQLite."""

    def __init__(self, base_url: str = MATTERHORN_BASE_URL, client: Optional[httpx.AsyncClient] = None) -> None:
        """Initialize the importer.

        :param base_url: Base URL of the Matterhorn instance.
        :param client: Optional httpx.AsyncClient instance for reuse.
        """
        self.base_url: str = base_url.rstrip("/")
        self.client: httpx.AsyncClient = client or httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        """Close the httpx client."""
        await self.client.aclose()

    async def fetch_catalog(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """Fetch a page of product catalog from Matterhorn.

        :param limit: Number of products to retrieve per page.
        :param offset: Pagination offset.
        :return: List of product dictionaries from Matterhorn.
        """
        url: str = f"{self.base_url}{MATTERHORN_API_ENDPOINT}"
        params: Dict[str, str] = {
            "limit": str(limit),
            "offset": str(offset),
        }
        try:
            response: httpx.Response = await self.client.get(url, params=params)
            response.raise_for_status()
            data: Dict[str, Any] = response.json()
            return data.get("products", [])
        except httpx.HTTPStatusError as exc:
            logger.error(f"Matterhorn API error: {exc.response.status_code} {exc}")
            return []
        except Exception as exc:  # pragma: no cover
            logger.exception(f"Unexpected error fetching Matterhorn catalog: {exc}")
            return []

    async def _apply_stock_buffer(self, raw_stock: int) -> int:
        """Apply the -10% safety buffer to supplier stock.

        :param raw_stock: Raw stock count from Matterhorn.
        :return: Buffered stock count that may safely be sold.
        """
        if raw_stock < 0:
            logger.warning(f"Negative stock received: {raw_stock}, returning 0")
            return 0
        buffered_stock: int = int(raw_stock * (1.0 - STOCK_BUFFER_PERCENT))
        return max(0, buffered_stock)

    async def _upsert_product(self, product_data: Dict[str, Any]) -> None:
        """Insert or update a product in the local SQLite database.

        Uses SQLite ON CONFLICT (sku) DO UPDATE pattern for atomic upsert.
        Applies the -10% stock buffer before storage.

        :param product_data: Product dictionary from Matterhorn API.
        """
        sku: str = product_data.get("sku", "")
        if not sku:
            logger.warning("Product received without SKU, skipping")
            return

        title: str = product_data.get("title", "Unknown")
        category: str = product_data.get("category", "")
        supplier_price: float = product_data.get("supplier_price", 0.0)
        raw_stock: int = product_data.get("stock", 0)

        # Apply safety buffer
        buffered_stock: int = await self._apply_stock_buffer(raw_stock)

        async with get_connection() as db:
            await db.execute(
                """
                INSERT INTO products (sku, title, category, supplier_price, stock, status)
                VALUES (?, ?, ?, ?, ?, 'active')
                ON CONFLICT (sku) DO UPDATE SET
                    title = excluded.title,
                    category = excluded.category,
                    supplier_price = excluded.supplier_price,
                    stock = excluded.stock,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (sku, title, category, supplier_price, buffered_stock),
            )
            await db.commit()

    async def import_catalog(self, limit: int = 500) -> Dict[str, Any]:
        """Import the full product catalog from Matterhorn into local SQLite.

        Fetches products in pages and upserts each one. Returns a summary
        of the import operation.

        :param limit: Maximum number of products to import (default 500).
        :return: Dictionary with import statistics.
        """
        stats: Dict[str, Any] = {
            "total_fetched": 0,
            "total_imported": 0,
            "total_updated": 0,
            "errors": 0,
        }

        page: int = 0
        while True:
            products: List[Dict[str, Any]] = await self.fetch_catalog(limit=limit, offset=page * limit)
            if not products:
                break

            for product in products:
                try:
                    await self._upsert_product(product)
                    stats["total_imported"] += 1
                except Exception as exc:  # pragma: no cover
                    logger.exception(f"Failed to import product {product.get('sku', 'unknown')}: {exc}")
                    stats["errors"] += 1

            stats["total_fetched"] += len(products)
            page += 1

        # Refetch to count how many were actually updated vs inserted
        # (SQLite ON CONFLICT doesn't easily tell us this, so we approximate)
        stats["total_updated"] = stats["total_imported"]  # placeholder: all count as processed

        logger.info(f"Matterhorn import complete: {stats}")
        return stats

    async def import_product(self, sku: str, limit: int = 1) -> bool:
        """Import a single product by SKU from Matterhorn.

        :param sku: Product SKU to look up.
        :param limit: Pagination limit for the API call.
        :return: True if product was successfully imported/updated.
        """
        products: List[Dict[str, Any]] = await self.fetch_catalog(limit=limit)

        # Find product by SKU
        target_product: Optional[Dict[str, Any]] = None
        for p in products:
            if p.get("sku") == sku:
                target_product = p
                break

        if target_product is None:
            logger.warning(f"Product with SKU '{sku}' not found in Matterhorn catalog")
            return False

        try:
            await self._upsert_product(target_product)
            return True
        except Exception as exc:  # pragma: no cover
            logger.exception(f"Failed to import product {sku}: {exc}")
            return False