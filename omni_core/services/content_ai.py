"""Content AI Engine for Omni-Core E-Commerce Middleware.

Provides AI-powered content generation and analysis using the Omni-Route Gateway.
This service transforms raw product data into high-quality E-Commerce copy
and performs trend/pricing analysis.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from .omni_router_client import OmniRouterClient

logger = logging.getLogger(__name__)

# Default prompts for content generation
DEFAULT_PROMPTS: dict[str, str] = {
    "product_description": (
        "You are an expert E-Commerce copywriter specializing in fashion retail. "
        "Generate a compelling product description that drives sales. "
        "The description should be engaging, benefit-focused, and optimized for conversion."
    ),
    "trend_analysis": (
        "You are a fashion trend analyst with deep knowledge of the European market. "
        "Analyze the following products and identify trends, seasonality, and pricing strategy. "
        "Consider current market conditions, competitor pricing, and seasonal patterns."
    ),
}


class ContentAIEngine:
    """
    AI-powered content generation and analysis engine for E-Commerce products.

    Uses the Omni-Route Gateway to transform raw product data into
    high-quality marketing copy and perform intelligent pricing analysis.

    The engine is designed to work with the -10% stock buffer logic
    and integrates seamlessly with the Omni-Core Middleware pipeline.
    """

    def __init__(self, omni_router_client: OmniRouterClient | None = None):
        """
        Initialize the ContentAIEngine.

        :param omni_router_client: An initialized OmniRouterClient instance.
            If None, a default client will be created.
        """
        self.omni_router: OmniRouterClient = omni_router_client or OmniRouterClient()

    # -------------------------------------------------------------------------
    # Product Description Optimization
    # -------------------------------------------------------------------------

    async def optimize_product_description(
        self,
        raw_title: str,
        raw_description: str,
        category: str,
    ) -> dict[str, str]:
        """
        Generate SEO-optimized product copy from raw product data.

        Creates:
        - A compelling, sales-focused title (max 60 characters for SEO)
        - An emotional, benefit-focused product description
        - Key features bullet points
        - Meta tags (title and description for search engines)

        :param raw_title: Original product title from the supplier
        :param raw_description: Raw description text from the supplier
        :param category: Product category (used for context-sensitive generation)
        :return: Dictionary with optimized copy components
        """
        system_prompt: str = (
            "You are an expert E-Commerce copywriter specializing in fashion retail. "
            "Generate compelling, conversion-focused product copy. "
            "Always output valid JSON with the following structure: "
            "{'seo_title': '...', 'description': '...', 'features': ['...'], 'meta_tags': {'title': '...', 'description': '...'}}"
        )

        user_prompt: str = (
            f"Product Title: '{raw_title}'\n"
            f"Product Description: '{raw_description}'\n"
            f"Category: {category}\n"
            "Generate the following output as valid JSON:\n"
            "- seo_title: A concise, keyword-rich title (max 60 chars) for SEO\n"
            "- description: A compelling, benefit-focused product description (150-300 chars)\n"
            "- features: A list of 3-5 key features/benefits\n"
            "- meta_tags: An object with 'title' (max 60 chars) and 'description' (150-160 chars) for meta tags"
        )

        try:
            response: dict[str, Any] | None = await self.omni_router.get_structured_response(
                system_prompt, user_prompt, response_format={"type": "json_object"}
            )

            if response and isinstance(response, dict):
                return {
                    "seo_title": response.get("seo_title", raw_title[:60]),
                    "description": response.get("description", raw_description),
                    "features": response.get("features", []),
                    "meta_tags": response.get("meta_tags", {
                        "title": raw_title[:60],
                        "description": raw_description[:160],
                    }),
                }
        except Exception as e:
            logger.warning(f"AI description generation failed: {e}")

        # Fallback: generate basic copy from raw data
        return {
            "seo_title": raw_title[:60],
            "description": raw_description or raw_title,
            "features": self._extract_basic_features(raw_title, raw_description),
            "meta_tags": {
                "title": raw_title[:60],
                "description": (raw_description or raw_title)[:160],
            },
        }

    @staticmethod
    def _extract_basic_features(title: str, description: str) -> list[str]:
        """Extract basic feature keywords from title and description."""
        features: list[str] = []
        if title:
            features.append(title[:50])
        if description:
            # Extract first few sentences as feature-like points
            sentences = description.split(". ")[:3]
            for sentence in sentences:
                if sentence.strip():
                    features.append(sentence.strip()[:80])
        return features or ["Hochwertiges Produkt"]

    # -------------------------------------------------------------------------
    # Trend & Pricing Analysis
    # -------------------------------------------------------------------------

    async def analyze_trends_and_pricing(
        self,
        products_batch: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Analyze a batch of imported products and suggest pricing/trend insights.

        Considers:
        - The -10% stock buffer logic (products with low buffered stock get higher margin suggestions)
        - Category-based trend analysis
        - Cost-plus pricing with margin calculation

        :param products_batch: List of product dictionaries from the import pipeline.
            Each product should have: 'supplier_price', 'stock', 'category', 'sku'
        :return: List of product dictionaries with added 'trend_score', 'suggested_price',
            'margin_recommendation', and 'restock_recommendation' fields.
        """
        if not products_batch:
            return []

        results: list[dict[str, Any]] = []

        for product in products_batch:
            try:
                product_result: dict[str, Any] = {
                    "sku": product.get("sku", "unknown"),
                    "category": product.get("category", "unknown"),
                    "supplier_price": product.get("supplier_price", 0.0),
                    "stock": product.get("stock", 0),
                    "trend_score": 0.0,
                    "suggested_price": 0.0,
                    "margin_recommendation": "normal",
                    "restock_recommendation": "normal",
                }

                # Apply the -10% stock buffer logic
                raw_stock: int = product.get("stock", 0)
                buffered_stock: int = int(raw_stock * 0.90) if raw_stock > 0 else 0

                # Calculate margin recommendations based on stock level
                supplier_price: float = product.get("supplier_price", 0.0)
                margin_percent: float = 0.25  # 25% default margin

                # Base cost calculation
                base_cost: float = supplier_price

                # Margin calculation with stock consideration
                if buffered_stock <= 0:
                    # Out of stock - higher margin, focus on revenue recovery
                    product_result["margin_recommendation"] = "high_recovery"
                    margin_adjustment: float = 0.35  # 35% margin for slow-moving stock
                elif buffered_stock < 10:
                    # Low stock - moderate margin
                    product_result["margin_recommendation"] = "normal"
                    margin_adjustment: float = 0.25
                else:
                    # Good stock level - competitive margin
                    product_result["margin_recommendation"] = "competitive"
                    margin_adjustment: float = 0.18

                # Calculate suggested retail price
                suggested_price: float = round(supplier_price / (1 - margin_adjustment), 2)

                # Trend score based on stock level and price competitiveness
                if buffered_stock > 50:
                    product_result["trend_score"] = 7.0  # High trend potential (overstock, need clearance)
                elif buffered_stock > 10:
                    product_result["trend_score"] = 5.0  # Moderate trend potential
                else:
                    product_result["trend_score"] = 2.0  # Low stock, niche product

                product_result["suggested_price"] = suggested_price
                product_result["restock_recommendation"] = (
                    "urgent" if buffered_stock <= 0
                    else "moderate" if buffered_stock < 10
                    else "normal"
                )

                results.append(product_result)

            except Exception as e:
                logger.warning(f"Trend/pricing analysis failed for product: {e}")
                results.append({
                    "sku": product.get("sku", "unknown"),
                    "category": product.get("category", "unknown"),
                    "trend_score": 0.0,
                    "suggested_price": product.get("supplier_price", 0.0),
                    "margin_recommendation": "normal",
                    "restock_recommendation": "normal",
                })

        return results

    async def batch_optimize_descriptions(
        self,
        products_data: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Optimize descriptions for a batch of products concurrently.

        :param products_data: List of product dicts with 'raw_title', 'raw_description', 'category'
        :return: List of product dicts with added 'optimized_title', 'ai_description', 'features', 'meta_tags'
        """
        if not products_data:
            return []

        # Create tasks for concurrent processing
        tasks: list = []
        for product_data in products_data:
            task = asyncio.create_task(
                self.optimize_product_description(
                    raw_title=product_data.get("raw_title", ""),
                    raw_description=product_data.get("raw_description", ""),
                    category=product_data.get("category", ""),
                )
            )
            tasks.append((product_data, task))

        # Wait for all tasks to complete
        results: list[dict[str, Any]] = []
        for product_data, task in tasks:
            try:
                optimized: dict[str, str] = await task
                result = dict(product_data)
                result["optimized_title"] = optimized.get("seo_title", product_data.get("raw_title", ""))
                result["ai_description"] = optimized.get("description", product_data.get("raw_description", ""))
                result["features"] = optimized.get("features", [])
                result["meta_tags"] = optimized.get("meta_tags", {})
                results.append(result)
            except Exception as e:
                logger.warning(f"Failed to optimize description for product: {e}")
                # Fallback: keep original data
                result = dict(product_data)
                result["optimized_title"] = product_data.get("raw_title", "")
                result["ai_description"] = product_data.get("raw_description", "")
                result["features"] = []
                result["meta_tags"] = {}
                results.append(result)

        return results