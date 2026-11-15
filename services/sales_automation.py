"""Sales Automation Module for Omni-Core ERP & BI Enterprise Edition.

Provides intelligent sales automation including:
- Conversion optimization scoring
- Fast-moving inventory detection
- Smart pricing rules with margin protection
- Cart/lead tracking integration
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from services.omni_router_client import OmniRouterClient

logger = logging.getLogger("omni.sales")


class SalesAutomationEngine:
    """Intelligent sales automation engine for conversion optimization and pricing."""

    def __init__(self, client: OmniRouterClient | None = None) -> None:
        self.client = client or OmniRouterClient()
        # Smart pricing configuration
        self.min_margin_pct = 15.0  # Minimum gross margin percentage
        self.stock_decay_factor = 0.90  # -10% buffer for buffered stock
        self.velocity_window_days = 30  # Sales velocity observation window

    async def calculate_conversion_score(self, product_sku: str, db_session) -> Dict[str, Any]:
        """Calculate a conversion optimization score for a product (0-100)."""

        from services.database import get_connection, close_connection

        async with get_connection() as db:
            # Get product data
            product_result = await db.execute(
                "SELECT sku, title, stock, buffered_stock, retail_price, supplier_price, "
                "status FROM products WHERE sku = :sku",
                {"sku": product_sku},
            )
            product = product_result.fetchone()

            if not product:
                return {"score": 0, "reason": "Product not found", "category": "unknown"}

            # Get recent order velocity
            order_result = await db.execute(
                """
                SELECT COUNT(*) as order_count, 
                       AVG(quantity) as avg_quantity
                FROM orders 
                WHERE sku = :sku 
                AND created_at >= NOW() - INTERVAL '30 days'
                """,
                {"sku": product_sku},
            )
            order_data = order_result.fetchone()

            # Calculate velocity score (0-40 points)
            velocity_score = 0
            if order_data and order_data.order_count > 0:
                velocity_score = min(40, float(order_data.order_count) * 5)

            # Calculate stock/sale urgency score (0-30 points)
            stock_score = 0
            if product and product.stock > 0:
                # Fast-moving stock gets higher score
                stock_ratio = float(product.buffered_stock) / max(1, float(product.stock) or 1)
                if stock_ratio < 0.2:
                    stock_score = 30  # Very low stock - high urgency
                elif stock_ratio < 0.5:
                    stock_score = 20  # Low stock
                elif stock_ratio < 0.8:
                    stock_score = 10  # Medium stock

            # Calculate price competitiveness score (0-30 points)
            price_score = 0
            if product and product.retail_price and product.supplier_price > 0:
                margin_pct = ((float(product.retail_price) - float(product.supplier_price)) / float(product.retail_price)) * 100
                if margin_pct >= self.min_margin_pct:
                    # Good margin - score based on how competitive
                    price_score = min(30, int(margin_pct / 5) * 3)

            # Calculate status bonus (0-10 points)
            status_score = 0
            if product and product.status == "active":
                status_score = 10

            total_score = velocity_score + stock_score + price_score + status_score

            return {
                "score": min(100, total_score),
                "velocity_score": velocity_score,
                "stock_score": stock_score,
                "price_score": price_score,
                "status_score": status_score,
                "details": {
                    "recent_orders_30d": float(order_data.order_count) if order_data else 0,
                    "buffered_stock": float(product.buffered_stock) if product else 0,
                    "current_stock": float(product.stock) if product else 0,
                    "margin_pct": round(((float(product.retail_price) - float(product.supplier_price)) / float(product.retail_price)) * 100, 2) if product and product.retail_price and product.supplier_price > 0 else 0,
                    "status": product.status if product else "unknown",
                },
                "calculated_at": datetime.utcnow().isoformat(),
            }

    async def smart_pricing_rule(
        self, product_sku: str, db_session, market_data: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Apply smart pricing rules with margin protection based on stock and -10% buffer."""

        from services.database import get_connection, close_connection

        async with get_connection() as db:
            # Get product data
            product_result = await db.execute(
                """
                SELECT sku, title, stock, buffered_stock, retail_price, supplier_price, 
                       status, ai_meta
                FROM products WHERE sku = :sku
                """,
                {"sku": product_sku},
            )
            product = product_result.fetchone()

            if not product:
                return {"error": "Product not found", "sku": product_sku}

            retail_price = float(product.retail_price) if product.retail_price else 0.0
            supplier_price = float(product.supplier_price) if product.supplier_price else 0.0
            stock = float(product.stock) if product.stock else 0
            buffered_stock = float(product.buffered_stock) if product.buffered_stock else 0
            margin_pct = ((retail_price - supplier_price) / retail_price) * 100 if retail_price > 0 else 0

            # Determine current market position
            pricing_action = "hold"
            new_price = retail_price
            reason = "No action needed - current price is optimal"

            # Rule 1: Stock below 20% of buffered stock - protect margin
            if stock > 0 and buffered_stock / stock < 0.2 and margin_pct < 25:
                # Low stock + thin margin - increase price to protect margin
                price_increase_pct = max(5.0, (25 - margin_pct) * 0.5)
                new_price = round(retail_price * (1 + price_increase_pct / 100), 2)
                pricing_action = "increase"
                reason = f"Low stock alert: {stock:.0f} units stock, margin {margin_pct:.1f}% - price increased {price_increase_pct:.1f}% to protect margin"

            # Rule 2: Stock well above buffered stock - competitive pricing
            elif stock > 0 and buffered_stock / stock > 1.5 and margin_pct > 25:
                # Good stock level, can afford competitive pricing
                price_reduction_pct = min(8.0, (margin_pct - 20) * 0.3)
                new_price = round(retail_price * (1 - price_reduction_pct / 100), 2)
                pricing_action = "decrease"
                reason = f"Good stock level: {stock:.0f} units - competitive pricing applied {price_reduction_pct:.1f}% margin buffer"

            # Rule 3: Margin below minimum - force price increase
            elif margin_pct < self.min_margin_pct:
                price_increase_pct = max(10.0, (self.min_margin_pct - margin_pct) * 2 + 5)
                new_price = round(retail_price * (1 + price_increase_pct / 100), 2)
                pricing_action = "increase_mandatory"
                reason = f"Margin protection: current {margin_pct:.1f}% below minimum {self.min_margin_pct:.1f}% - mandatory price increase of {price_increase_pct:.1f}%"

            # Rule 4: High velocity, good stock, optimize price
            elif stock > 0 and buffered_stock / stock < 0.5:
                # Fast-moving, low stock - slight price optimization
                optimization_pct = min(3.0, (50 - margin_pct) * 0.1)
                new_price = round(retail_price * (1 - optimization_pct / 100), 2) if margin_pct > 25 else retail_price
                pricing_action = "optimize"
                reason = f"High velocity, low stock - price optimized {optimization_pct:.1f}% for conversion balance"

            # Apply price update to database
            if pricing_action != "hold" and new_price != retail_price:
                await db.execute(
                    """
                    UPDATE products 
                    SET retail_price = :new_price, updated_at = NOW()
                    WHERE sku = :sku
                    """,
                    {"new_price": new_price, "sku": product_sku},
                )
                await db.commit()

                # Log the price adjustment
                await db.execute(
                    """
                    INSERT INTO system_logs (event_type, event_data, sku, channel_name)
                    VALUES ('price_adjustment', :event_data, :sku, 'internal')
                    """,
                    {
                        "event_data": json.dumps({
                            "action": pricing_action,
                            "old_price": retail_price,
                            "new_price": new_price,
                            "margin_pct": round(margin_pct, 2),
                            "stock": stock,
                            "trigger": reason
                        }),
                        "sku": product_sku,
                    },
                )
                await db.commit()

            return {
                "sku": product_sku,
                "action": pricing_action,
                "old_price": retail_price,
                "new_price": new_price,
                "margin_pct": round(margin_pct, 2),
                "reason": reason,
                "buffered_stock_ratio": round(buffered_stock / max(1, stock), 3) if stock > 0 else 0,
                "timestamp": datetime.utcnow().isoformat(),
            }

    async def detect_fast_moving_stock(self, db_session, top_n: int = 10) -> List[Dict[str, Any]]:
        """Detect the top N fast-moving products based on order velocity."""

        from services.database import get_connection, close_connection

        async with get_connection() as db:
            result = await db.execute(
                """
                SELECT p.sku, p.title, p.retail_price, p.supplier_price, 
                       p.stock, p.buffered_stock,
                       COALESCE(o.order_count, 0) as order_count_30d,
                       COALESCE(o.total_revenue, 0) as revenue_30d
                FROM products p
                LEFT JOIN (
                    SELECT sku, 
                           COUNT(*) as order_count,
                           SUM(quantity * retail_price) as total_revenue
                    FROM orders 
                    WHERE created_at >= NOW() - INTERVAL '30 days'
                    GROUP BY sku
                ) o ON p.sku = o.sku
                WHERE p.status = 'active'
                ORDER BY o.order_count DESC NULLS LAST
                LIMIT :limit
                """,
                {"limit": top_n},
            )
            rows = result.fetchall()

            fast_movers = []
            for row in rows:
                margin_pct = ((float(row.retail_price) - float(row.supplier_price)) / float(row.retail_price)) * 100 if float(row.retail_price) > 0 else 0
                fast_movers.append({
                    "sku": row.sku,
                    "title": row.title,
                    "revenue_30d": round(float(row.revenue_30d), 2),
                    "orders_30d": int(row.order_count_30d),
                    "stock": int(row.stock),
                    "buffered_stock": int(row.buffered_stock),
                    "margin_pct": round(margin_pct, 2),
                    "velocity_score": min(100, int(row.order_count_30d) * 3 + 20),
                    "recommendation": self._get_restock_recommendation(int(row.buffered_stock), int(row.stock)),
                })

            return fast_movers

    def _get_restock_recommendation(self, buffered: int, current: int) -> str:
        """Get restock recommendation based on stock levels."""
        if current == 0:
            return "out_of_stock - immediate restock required"
        ratio = current / max(1, buffered)
        if ratio < 0.3:
            return "critical - restock urgently needed"
        elif ratio < 0.5:
            return "warning - restock recommended soon"
        elif ratio < 0.8:
            return "normal - monitor stock levels"
        else:
            return "healthy - stock levels optimal"

    async def generate_sales_report(self, db_session, period_days: int = 30) -> Dict[str, Any]:
        """Generate a comprehensive sales automation report."""

        from services.database import get_connection, close_connection

        async with get_connection() as db:
            # Overall metrics
            metrics_result = await db.execute(
                """
                SELECT 
                    COUNT(*) as total_products,
                    COUNT(*) FILTER (WHERE status = 'active') as active_products,
                    COUNT(*) FILTER (WHERE stock <= 0) as out_of_stock,
                    COUNT(*) FILTER (WHERE buffered_stock / NULLIF(stock, 0) < 0.3) as critical_stock,
                    SUM(retail_price * stock) as potential_revenue,
                    SUM(supplier_price * stock) as potential_cost
                FROM products
                """
            )
            metrics = metrics.fetchone()

            # Top fast movers
            fast_movers = await self.detect_fast_moving_stock(db, top_n=5)

            # Products needing price adjustment
            pricing_result = await db.execute(
                """
                SELECT sku, title, retail_price, supplier_price, stock, buffered_stock
                FROM products
                WHERE status = 'active'
                AND retail_price > 0
                AND (retail_price / NULLIF(supplier_price, 0) - 1) * 100 < 15
                """
            )
            pricing_rows = pricing_result.fetchall()

            return {
                "period_days": period_days,
                "executive_summary": {
                    "total_products": int(metrics.total_products) if metrics else 0,
                    "active_products": int(metrics.active_products) if metrics else 0,
                    "out_of_stock": int(metrics.out_of_stock) if metrics else 0,
                    "critical_stock_items": int(metrics.critical_stock) if metrics else 0,
                    "potential_total_revenue": round(float(metrics.potential_revenue) if metrics.potential_revenue else 0, 2),
                    "potential_total_cost": round(float(metrics.potential_cost) if metrics.potential_cost else 0, 2),
                },
                "top_fast_movers": fast_movers,
                "price_adjustment_candidates": [
                    {
                        "sku": row.sku,
                        "title": row.title,
                        "current_price": float(row.retail_price),
                        "supplier_price": float(row.supplier_price),
                        "current_margin_pct": round(((float(row.retail_price) - float(row.supplier_price)) / float(row.retail_price)) * 100, 2),
                        "stock_ratio": round(float(row.buffered_stock) / max(1, float(row.stock)), 3),
                    }
                    for row in pricing_rows
                ],
                "automation_actions_recommended": len(fast_movers) + len(pricing_rows),
                "generated_at": datetime.utcnow().isoformat(),
            }


# Convenience function for FastAPI dependency injection
def get_sales_automation_engine() -> SalesAutomationEngine:
    """Factory function for SalesAutomationEngine instance."""
    return SalesAutomationEngine()