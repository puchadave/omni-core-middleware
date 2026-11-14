"""Async Matterhorn Universal Importer for Omni-Core E-Commerce Middleware.

Provides functionality to fetch product data from Matterhorn via REST API
and XML Offer Feed, apply stock safety buffer, and persist to SQLite
via UPSERT semantics.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import elementtree.ElementTree as ET
import httpx
import aiosqlite

from database import get_connection, close_connection, DB_PATH

logger = logging.getLogger(__name__)

# Matterhorn API endpoints (configuration-based)
MATTERHORN_REST_API: str = "https://matterhorn-moda.de"
MATTERHORN_XML_FEED: str = "https://matterhorn-moda.de/?str=xmloffer"

# Stock safety buffer: subtract 10% from raw supplier stock to prevent overselling
STOCK_BUFFER_PERCENT: float = 0.10


class MatterhornUniversalImporter:
    """Universal importer for Matterhorn wholesale product data.

    Supports two data acquisition methods:
    1. REST API with Bearer token authentication
    2. XML Offer Feed parsing

    All data is persisted to SQLite via atomic UPSERT operations.
    """

    def __init__(self, api_token: str | None = None, client: Optional[httpx.AsyncClient] = None) -> None:
        """Initialize the importer.

        :param api_token: Bearer token for REST API authentication.
            If None, will be read from the settings table in the database.
        :param client: Optional httpx.AsyncClient instance for connection reuse.
        """
        self.api_token: str | None = api_token
        self.client: httpx.AsyncClient = client or httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        """Close the httpx client."""
        await self.client.aclose()

    async def _get_api_token(self) -> str | None:
        """Read the API token from the database settings table.

        :return: The bearer token value, or None if not configured.
        """
        async with aiosqlite.connect(str(DB_PATH)) as db:
            async with db.execute("SELECT value FROM settings WHERE key='matterhorn_api_token'") as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    # -------------------------------------------------------------------------
    # Method 1: REST API Fetch
    # -------------------------------------------------------------------------

    async def fetch_via_rest_api(self, endpoint: str = "/api/v1/products", params: dict | None = None) -> List[Dict[str, Any]]:
        """Fetch product data from Matterhorn REST API.

        :param endpoint: API endpoint path (default: /api/v1/products)
        :param params: Query parameters to append to the request
        :return: List of product dictionaries from the API response
        """
        token: str | None = self.api_token or await self._get_api_token()
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        url: str = f"{MATTERHORN_REST_API}{endpoint}"
        try:
            response: httpx.Response = await self.client.get(url, headers=headers, params=params or {})
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            # Expected response structure: {"products": [...]} or direct list
            products: List[Dict[str, Any]] = data.get("products", [])
            if isinstance(products, list):
                return products
            return []
        except httpx.HTTPStatusError as exc:
            logger.error(f"Matterhorn REST API error {exc.response.status_code}: {exc}")
            return []
        except Exception:  # pragma: no cover
            logger.exception(f"Unexpected error fetching from Matterhorn REST API: {exc}")
            return []

    # -------------------------------------------------------------------------
    # Method 2: XML Offer Feed Parsing
    # -------------------------------------------------------------------------

    async def fetch_via_xml_offer(self) -> List[Dict[str, Any]]:
        """Parse the XML Offer Feed from Matterhorn.

        Reads the XML feed from https://matterhorn-moda.de/?str=xmloffer
        and extracts product information: SKU, Title, Price, Category, Stock.

        :return: List of product dictionaries with keys: sku, title, price,
                 category, raw_stock
        """
        url: str = MATTERHORN_XML_FEED
        try:
            response: httpx.Response = await self.client.get(url, timeout=30.0)
            response.raise_for_status()

            # Parse XML with elementtree
            root: ET.Element = ET.fromstring(response.text)

            # Expected XML structure (based on Matterhorn offer format):
            products: List[Dict[str, Any]] = []

            # Find all product elements (adjust XPath based on actual XML structure)
            product_elements = root.findall(".//product")

            if not product_elements:
                # Fallback: try direct children of products root
                product_elements = root.findall("product")

            for prod_elem in product_elements:
                try:
                    # Extract fields - adjust element names based on actual XML structure
                    sku_elem = prod_elem.find("sku")
                    title_elem = prod_elem.find("title")
                    price_elem = prod_elem.find("price")
                    category_elem = prod_elem.find("category")
                    stock_elem = prod_elem.find("stock")

                    sku: str = (sku_elem.text or "").strip() if sku_elem is not None else ""
                    title: str = (title_elem.text or "Untitled Product").strip() if title_elem is not None else "Untitled Product"
                    price: float = float(price_elem.text or "0.0") if price_elem is not None else 0.0
                    category: str = (category_elem.text or "").strip() if category_elem is not None else ""
                    raw_stock: int = int(stock_elem.text or "0") if stock_elem is not None else 0

                    # Apply safety buffer
                    buffered_stock: int = int(raw_stock * (1.0 - STOCK_BUFFER_PERCENT))

                    product_data: Dict[str, Any] = {
                        "sku": sku,
                        "title": title,
                        "price": price,
                        "category": category,
                        "raw_stock": raw_stock,
                        "buffered_stock": buffered_stock,
                    }
                    products.append(product_data)

                except (ValueError, TypeError) as exc:
                    logger.warning(f"Failed to parse product element: {exc}")
                    continue

            return products

        except httpx.HTTPStatusError as exc:
            logger.error(f"Matterhorn XML Feed error {exc.response.status_code}: {exc}")
            return []
        except Exception:  # pragma: no cover
            logger.exception(f"Unexpected error fetching from Matterhorn XML Feed: {exc}")
            return []

    # -------------------------------------------------------------------------
    # Core Import Logic: UPSERT into SQLite
    # -------------------------------------------------------------------------

    async def _upsert_product(self, product_data: Dict[str, Any]) -> None:
        """Insert or update a product in the local SQLite database.

        Uses SQLite ON CONFLICT (sku) DO UPDATE pattern for atomic upsert.
        Applies the -10% stock buffer before storage.

        :param product_data: Product dictionary from either fetch method,
            expected to contain at minimum: sku, title, price, raw_stock
        """
        sku: str = product_data.get("sku", "")
        if not sku:
            logger.warning("Product received without SKU, skipping")
            return

        title: str = product_data.get("title", "Unknown Product")
        price: float = product_data.get("price", 0.0)
        raw_stock: int = product_data.get("raw_stock", 0)
        category: str = product_data.get("category", "")

        # Apply safety buffer (10% buffer to prevent overselling)
        buffered_stock: int = int(raw_stock * (1.0 - STOCK_BUFFER_PERCENT))

        async with get_connection() as db:
            await db.execute(
                """
                INSERT INTO products (sku, title, category, supplier_price, stock, status)
                VALUES (?, ?, ?, ?, 'active')
                ON CONFLICT (sku) DO UPDATE SET
                    title = excluded.title,
                    category = excluded.category,
                    supplier_price = excluded.supplier_price,
                    stock = excluded.buffered_stock,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (sku, title, category, price, buffered_stock),
            )
            await db.commit()

    # -------------------------------------------------------------------------
    # Full Import Orchestration
    # -------------------------------------------------------------------------

    async def import_from_rest(self, endpoint: str = "/api/v1/products", params: dict | None = None) -> Dict[str, Any]:
        """Full import cycle via REST API: fetch and persist.

        :param endpoint: API endpoint path
        :param params: Query parameters for the API request
        :return: Dictionary with import statistics
        """
        products: List[Dict[str, Any]] = await self.fetch_via_rest_api(endpoint, params)
        stats: Dict[str, Any] = {"fetched": 0, "imported": 0, "errors": 0}

        for product in products:
            try:
                await self._upsert_product(product)
                stats["imported"] += 1
            except Exception:  # pragma: no cover
                logger.exception(f"Failed to import product: {exc}")
                stats["errors"] += 1

        stats["fetched"] = len(products)
        return stats

    async def import_from_xml_offer(self) -> Dict[str, Any]:
        """Full import cycle via XML Offer Feed: parse and persist.

        :return: Dictionary with import statistics
        """
        products: List[Dict[str, Any]] = await self.fetch_via_xml_offer()
        stats: Dict[str, Any] = {"fetched": 0, "imported": 0, "errors": 0}

        for product in products:
            try:
                # Ensure category key exists
                if "category" not in product:
                    product["category"] = ""
                await self._upsert_product(product)
                stats["imported"] += 1
            except Exception:  # pragma: no cover
                logger.exception(f"Failed to import product from XML: {exc}")
                stats["errors"] += 1

        stats["fetched"] = len(products)
        return stats

    async def import_single_product(self, sku: str) -> bool:
        """Import a single product by SKU, trying both methods.

        :param sku: The product SKU to import
        :return: True if product was successfully imported/updated
        """
        # Try REST API first
        products = await self.fetch_via_rest_api(params={"sku": sku})
        if products:
            product = products[0]
            if product.get("sku") == sku:
                await self._upsert_product(product)
                return True

        # Fallback: Try XML feed (less likely to have single product, but possible)
        products = await self.fetch_via_xml_offer()
        for product in products:
            if product.get("sku") == sku:
                await self._upsert_product(product)
                return True

        logger.warning(f"Product with SKU '{sku}' not found in either source")
        return False