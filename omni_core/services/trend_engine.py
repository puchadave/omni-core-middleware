import asyncio
import logging
import random
from typing import List, Dict, Any, Optional

from database import get_db
from services.omniroute_client import OmniRouteClient

logger = logging.getLogger(__name__)

CATEGORIES = ["Jacke", "Hose", "Oberteil", "Kleid", "Schuhe", "Accessoire"]
SEASONS = ["Frühling", "Sommer", "Herbst", "Winter"]
STYLES = ["Streetwear", "Business-Casual", "Sportlich", "Elegant", "Boho", "Minimalistisch"]


class TrendEngine:
    """
    Autonomous weekly background task:
    1. Queries OmniRoute for current fashion trends
    2. Matches trends against local inventory
    3. Generates exactly 10 new trend-based outfit bundles
    """

    def __init__(self, omniroute_client: OmniRouteClient):
        self.omniroute_client = omniroute_client

    async def run_weekly_trend_job(self) -> Dict[str, Any]:
        """Executes the full weekly trend analysis and bundle generation."""
        result = {
            "trends_found": 0,
            "bundles_created": 0,
            "errors": []
        }

        try:
            trends = await self._fetch_trends()
            if not trends:
                result["errors"].append("No trends returned from OmniRoute")
                return result

            result["trends_found"] = len(trends.get("trends", []))

            inventory = await self._load_inventory()
            if not inventory:
                result["errors"].append("Inventory empty - cannot build bundles")
                return result

            bundles = self._build_bundles(trends, inventory)
            for bundle in bundles:
                await self._persist_bundle(bundle)
                result["bundles_created"] += 1

            logger.info(f"Trend job complete: {result['bundles_created']} bundles created")

        except Exception as e:
            logger.exception(f"Trend job failed: {e}")
            result["errors"].append(str(e))

        return result

    async def _fetch_trends(self) -> Optional[Dict[str, Any]]:
        """Queries OmniRoute for current fashion trends."""
        prompt = (
            "Analysiere aktuelle Fashion-Trends 2026. "
            "Gib eine JSON-Antwort mit folgendem Schema zurück: "
            '{"trends": [{"name": "<Trend-Name>", "category": "<Jacke|Hose|Oberteil|Kleid|Schuhe|Accessoire>", '
            '"keywords": ["<...>"], "season": "<Frühling|Sommer|Herbst|Winter>", "style": "<Stil>"}]}. '
            "Liefere exakt 5 Trends."
        )
        return await self.omniroute_client.get_ai_trend_analysis(prompt)

    async def _load_inventory(self) -> List[Dict[str, Any]]:
        """Loads all active non-bundle products with stock > 0."""
        products: List[Dict[str, Any]] = []
        async with get_db() as db:
            async with db.execute(
                "SELECT sku, title, category, stock FROM products WHERE status = 'active' AND is_bundle = 0 AND stock > 0"
            ) as cursor:
                async for row in cursor:
                    products.append(dict(row))
        return products

    def _build_bundles(self, trends: Dict[str, Any], inventory: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Builds exactly 10 trend bundles from inventory.
        Each bundle contains 2-4 components matched to a trend category.
        """
        trend_list = trends.get("trends", [])
        if not trend_list:
            trend_list = [
                {"name": f"{style} Look", "category": cat, "keywords": [cat.lower()], "season": season, "style": style}
                for style in STYLES[:5]
                for cat, season in [(CATEGORIES[0], SEASONS[0])]
            ][:5]

        bundles: List[Dict[str, Any]] = []
        used_skus: set = set()
        bundle_count = 0

        for i, trend in enumerate(trend_list):
            if bundle_count >= 10:
                break
            category = trend.get("category", "Oberteil")
            matching = [
                p for p in inventory
                if p["category"] == category and p["sku"] not in used_skus
            ]
            if len(matching) < 2:
                # Fallback: any unused products
                matching = [p for p in inventory if p["sku"] not in used_skus]

            if len(matching) < 2:
                continue

            components = random.sample(matching, min(3, len(matching)))
            component_skus = [c["sku"] for c in components]
            used_skus.update(component_skus)

            bundle_sku = f"TREND-{i+1:03d}-{trend['name'][:8].upper().replace(' ', '-')}"
            bundles.append({
                "bundle_sku": bundle_sku,
                "component_skus": ",".join(component_skus),
                "title": f"{trend['name']} Outfit Bundle",
                "trend": trend.get("name", ""),
                "style": trend.get("style", ""),
                "season": trend.get("season", "")
            })
            bundle_count += 1

        # Pad to exactly 10 if AI returned fewer trends
        idx = 0
        while bundle_count < 10 and inventory:
            available = [p for p in inventory if p["sku"] not in used_skus]
            if len(available) < 2:
                break
            components = random.sample(available, min(3, len(available)))
            component_skus = [c["sku"] for c in components]
            used_skus.update(component_skus)
            bundle_sku = f"TREND-PAD-{bundle_count+1:03d}"
            bundles.append({
                "bundle_sku": bundle_sku,
                "component_skus": ",".join(component_skus),
                "title": f"Curated Outfit Bundle #{bundle_count+1}",
                "trend": "Generic",
                "style": "Mixed",
                "season": "All"
            })
            bundle_count += 1
            idx += 1

        return bundles[:10]

    async def _persist_bundle(self, bundle: Dict[str, Any]) -> None:
        """Inserts or updates a bundle and its parent product row (idempotent)."""
        async with get_db() as db:
            # Ensure parent product exists
            await db.execute(
                """
                INSERT INTO products (sku, title, category, supplier_price, stock, is_bundle, status)
                VALUES (?, ?, ?, 0, 0, 1, 'active')
                ON CONFLICT(sku) DO UPDATE SET title = excluded.title, is_bundle = 1, status = 'active'
                """,
                (bundle["bundle_sku"], bundle["title"], "Bundle")
            )
            # Upsert bundle mapping
            await db.execute(
                """
                INSERT INTO bundles (bundle_sku, component_skus, title)
                VALUES (?, ?, ?)
                ON CONFLICT(bundle_sku) DO UPDATE SET component_skus = excluded.component_skus, title = excluded.title
                """,
                (bundle["bundle_sku"], bundle["component_skus"], bundle["title"])
            )
            await db.commit()


async def start_trend_scheduler(omniroute_client: OmniRouteClient, interval_seconds: int = 604800) -> None:
    """Runs the trend job on a weekly interval (default 7 days)."""
    engine = TrendEngine(omniroute_client)
    while True:
        try:
            await engine.run_weekly_trend_job()
        except Exception as e:
            logger.exception(f"Trend scheduler iteration failed: {e}")
        await asyncio.sleep(interval_seconds)