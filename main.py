"""FastAPI core application for Omni-Core E-Commerce Middleware.

Provides the main entry point for the application with endpoints for:
- Master dashboard view and API endpoints
- Product catalog management with AI metadata
- System configuration management
- Background Matterhorn import task trigger
- Shopify CSV and Google Shopping XML export
- Auto-Optimizer Agent status and system logs
- AI-powered content generation triggers
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request, BackgroundTasks, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from database import init_db, get_connection, close_connection, DB_PATH
from services.matterhorn_importer import MatterhornUniversalImporter
from services.auto_optimizer import AutoOptimizerAgent

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Application Lifespan & Startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    """FastAPI lifespan context.

    - Initializes the SQLite database (WAL mode + all tables: products,
      channel_overrides, settings, system_logs)
    - Creates a MatterhornImporter instance
    - Starts the AutoOptimizerAgent as a background task
    - Yields control to the application
    - On shutdown: stops the agent, closes database connections cleanly
    """
    # Initialize database with all new tables
    await init_db()

    # Create importer instance (token can be set via /api/config later)
    importer = MatterhornUniversalImporter(use_ai=True)

    # Create and start the Auto-Optimizer Agent
    optimizer = AutoOptimizerAgent(content_ai_engine=importer.ai_engine)
    await optimizer.start()

    # Store in app state for access across routes
    app.state.importer = importer
    app.state.optimizer = optimizer

    logger.info("Omni-Core Middleware initialized with AutoOptimizerAgent")

    yield

    # Cleanup on shutdown
    await optimizer.stop()
    await app.state.importer.close()
    await close_connection(None)  # noqa: B008


# ---------------------------------------------------------------------------
# FastAPI Application Assembly
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Omni-Core E-Commerce Middleware",
    version="2.0.0",
    description="E-Commerce import middleware with Matterhorn integration, "
                "Omni-Route Gateway KI-Veredelung und erweiterbarem Datenbankschema",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

async def get_current_settings() -> dict[str, str]:
    """Read current configuration from the settings table."""
    async with get_connection() as db:
        settings: dict[str, str] = {}
        async with db.execute("SELECT key, value FROM settings") as cursor:
            async for key, value in cursor:
                settings[key] = value
    # Ensure defaults exist
    if "matterhorn_api_token" not in settings:
        settings["matterhorn_api_token"] = ""
    return settings


async def get_product_count_stats() -> dict[str, int]:
    """Get product statistics from the database."""
    async with get_connection() as db:
        async with db.execute("SELECT COUNT(*) FROM products WHERE status = 'active'") as cursor:
            active: int = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM products") as cursor:
            total: int = (await cursor.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM products WHERE status = 'active' AND optimized_title IS NOT NULL AND optimized_title != ''"
        ) as cursor:
            ai_optimized: int = (await cursor.fetchone())[0]
    return {"total": total, "active": active, "ai_optimized": ai_optimized}


# ---------------------------------------------------------------------------
# HTTP Route Handlers
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def master_dashboard(request: Request):
    """Render the main dashboard HTML page with product statistics."""
    stats = await get_product_count_stats()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "stats": stats},
    )


@app.get("/admin/products", response_class=HTMLResponse)
async def products_page(request: Request):
    """Render the product catalog page with full AI metadata and stock info."""
    async with get_connection() as db:
        async with db.execute(
            "SELECT sku, title, category, supplier_price, retail_price, stock, buffered_stock, "
            "optimized_title, ai_meta FROM products WHERE status = 'active' ORDER BY title ASC"
        ) as cursor:
            products = await cursor.fetchall()

    # Transform for template compatibility
    product_list = []
    for p in products:
        ai_meta_parsed = {}
        if p[8]:
            try:
                ai_meta_parsed = json.loads(p[8])
            except (json.JSONDecodeError, TypeError):
                pass
        product_list.append({
            "sku": p[0],
            "title": p[1],
            "category": p[2],
            "supplier_price": p[3] or 0.0,
            "retail_price": p[4] or 0.0,
            "stock": p[5] or 0,
            "buffered_stock": p[6] or 0,
            "optimized_title": p[7] or "",
            "ai_meta": ai_meta_parsed,
        })

    return templates.TemplateResponse(
        "products.html",
        {"request": request, "products": product_list},
    )


@app.get("/admin/product/{sku}", response_class=HTMLResponse)
async def product_detail(request: Request, sku: str):
    """Render the product detail page with channel-specific content."""
    async with get_connection() as db:
        async with db.execute(
            "SELECT sku, title, category, supplier_price, retail_price, stock, buffered_stock, "
            "optimized_title, ai_meta FROM products WHERE sku = ?",
            (sku,),
        ) as cursor:
            product = await cursor.fetchone()

    if not product:
        return JSONResponse({"error": "Product not found"}, status_code=404)

    ai_meta_parsed = {}
    if product[8]:
        try:
            ai_meta_parsed = json.loads(product[8])
        except (json.JSONDecodeError, TypeError):
            pass

    # Fetch channel content for this product
    channel_content: Dict[str, Dict[str, Any]] = {}
    async with get_connection() as db:
        for channel in ["shopify", "amazon", "google_shopping"]:
            async with db.execute(
                "SELECT optimized_title, description_html, seo_keywords, image_prompt, is_published "
                "FROM channel_content WHERE sku = ? AND channel_name = ?",
                (sku, channel),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    channel_content[channel] = {
                        "optimized_title": row[0] or "",
                        "description_html": row[1] or "",
                        "seo_keywords": json.loads(row[2]) if row[2] else [],
                        "image_prompt": row[3] or "",
                        "is_published": row[4],
                    }

    return templates.TemplateResponse(
        "product_detail.html",
        {
            "request": request,
            "product": {
                "sku": product[0],
                "title": product[1],
                "category": product[2],
                "supplier_price": product[3] or 0.0,
                "retail_price": product[4] or 0.0,
                "stock": product[5] or 0,
                "buffered_stock": product[6] or 0,
                "optimized_title": product[7] or "",
                "ai_meta": ai_meta_parsed,
            },
            "channel_content": channel_content,
        },
    )


@app.get("/admin/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Render the configuration page."""
    settings = await get_current_settings()
    return templates.TemplateResponse("settings.html", {"request": request, "settings": settings})


@app.post("/admin/settings")
async def save_settings(
    request: Request,
):
    """Save configuration settings from the UI.

    Expects form data with matterhorn_api_token and other config keys.
    """
    form = await request.form()
    async with get_connection() as db:
        for key in form:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, form[key]),
            )
        await db.commit()
    return JSONResponse({"status": "ok", "message": "Einstellungen gespeichert"})


@app.post("/api/import/matterhorn")
async def trigger_matterhorn_import(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Trigger the Matterhorn import as a background task.

    Starts the import process in the background so the API responds
    immediately. The import runs asynchronously and does not block
    other endpoint calls.

    The importer will try both REST API and XML Feed methods.
    AI-powered content optimization is applied automatically.
    """
    importer: MatterhornUniversalImporter = request.app.state.importer
    optimizer: AutoOptimizerAgent = request.app.state.optimizer

    # Start the import task in the background
    background_tasks.add_task(importer.import_from_rest, trigger_ai=True)

    # Also trigger a quick optimization cycle after import
    background_tasks.add_task(optimizer.run_cycle, force=True)

    return JSONResponse({
        "status": "started",
        "message": "Matterhorn import mit KI-Optimierung triggered in background",
        "methods": ["rest_api", "xml_offer"],
        "ai_optimization": "enabled",
    })


@app.get("/api/health")
async def health_check():
    """Simple health check endpoint including Omni-Route Gateway status."""
    health: Dict[str, Any] = {"status": "healthy", "database": "connected"}

    # Check Omni-Route Gateway connectivity
    try:
        from services.omni_router_client import OmniRouterClient
        client = OmniRouterClient()
        gateway_online = await client.health_check()
        health["omni_route_gateway"] = "online" if gateway_online else "offline"
        await client.close()
    except Exception:
        health["omni_route_gateway"] = "offline"

    # Verify database connectivity
    try:
        async with get_connection() as db:
            await db.execute("SELECT 1")
    except Exception:
        health["database"] = "disconnected"
        health["status"] = "unhealthy"

    return JSONResponse(health)


@app.get("/api/admin/products")
async def api_admin_products():
    """API endpoint returning all products with full metadata as JSON."""
    async with get_connection() as db:
        async with db.execute(
            "SELECT sku, title, category, supplier_price, retail_price, stock, buffered_stock, "
            "optimized_title, ai_meta FROM products WHERE status = 'active' ORDER BY title ASC"
        ) as cursor:
            rows = await cursor.fetchall()

    product_list = []
    for row in rows:
        ai_meta_parsed = {}
        if row[8]:
            try:
                ai_meta_parsed = json.loads(row[8])
            except (json.JSONDecodeError, TypeError):
                pass
        product_list.append({
            "sku": row[0],
            "title": row[1],
            "category": row[2],
            "supplier_price": row[3] or 0.0,
            "retail_price": row[4] or 0.0,
            "stock": row[5] or 0,
            "buffered_stock": row[6] or 0,
            "optimized_title": row[7] or "",
            "ai_meta": ai_meta_parsed,
        })

    return JSONResponse({"products": product_list, "count": len(product_list)})


@app.get("/api/admin/system-logs")
async def api_system_logs(
    limit: int = Query(default=20, ge=1, le=200),
):
    """API endpoint returning recent system logs/events."""
    events: List[Dict[str, Any]] = []

    async with get_connection() as db:
        async with db.execute(
            """
            SELECT event_type, event_data, created_at
            FROM system_logs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()

    for row in rows:
        event_data_parsed = {}
        if row[1]:
            try:
                event_data_parsed = json.loads(row[1])
            except (json.JSONDecodeError, TypeError):
                pass
        events.append({
            "event_type": row[0],
            "event_data": event_data_parsed,
            "created_at": str(row[2]) if row[2] else "",
        })

    return JSONResponse({"events": events, "count": len(events)})


@app.post("/api/optimize/ai")
async def trigger_ai_optimization(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Trigger AI-powered content optimization for all products.

    Runs the ContentAIEngine to generate optimized titles, descriptions,
    and SEO metadata for products that haven't been optimized yet.
    """
    importer: MatterhornUniversalImporter = request.app.state.importer

    async def _optimize_background():
        try:
            # Run AI optimization on recently imported products
            result = await importer.trigger_ai_optimization()
            logger.info(f"Background AI optimization completed: {result}")
        except Exception as e:
            logger.exception(f"Background AI optimization failed: {e}")

    background_tasks.add_task(_optimize_background)

    return JSONResponse({
        "status": "started",
        "message": "AI-Inhaltsoptimierung wurde im Hintergrund gestartet",
    })


@app.get("/api/stats/overview")
async def api_stats_overview():
    """API endpoint returning overview statistics for the dashboard."""
    async with get_connection() as db:
        # Total products
        async with db.execute("SELECT COUNT(*) FROM products") as cursor:
            total_products: int = (await cursor.fetchone())[0]

        # Active products
        async with db.execute("SELECT COUNT(*) FROM products WHERE status = 'active'") as cursor:
            active_products: int = (await cursor.fetchone())[0]

        # Products with AI optimization
        async with db.execute(
            "SELECT COUNT(*) FROM products WHERE status = 'active' AND optimized_title IS NOT NULL AND optimized_title != ''"
        ) as cursor:
            ai_optimized_count: int = (await cursor.fetchone())[0]

        # Total buffered stock
        async with db.execute("SELECT COALESCE(SUM(buffered_stock), 0) FROM products WHERE status = 'active'") as cursor:
            total_buffered_stock: int = (await cursor.fetchone())[0]

        # Recent system events count
        async with db.execute("SELECT COUNT(*) FROM system_logs") as cursor:
            total_events: int = (await cursor.fetchone())[0]

    return JSONResponse({
        "total_products": total_products,
        "active_products": active_products,
        "ai_optimized_count": ai_optimized_count,
        "total_buffered_stock": total_buffered_stock,
        "total_system_events": total_events,
    })


# ---------------------------------------------------------------------------
# Development / Direct Execution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, loop="uvloop")