"""FastAPI core application for Omni-Core E-Commerce Middleware.

Provides the main entry point for the application with endpoints for:
- Master dashboard view and API endpoints
- System configuration management
- Background Matterhorn import task trigger
- Shopify CSV and Google Shopping XML export
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
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from database import init_db, get_connection, close_connection, DB_PATH
from services.matterhorn_importer import MatterhornUniversalImporter

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Application Lifespan & Startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    """FastAPI lifespan context.

    - Initializes the SQLite database (WAL mode + tables)
    - Creates a MatterhornImporter instance (without token; token can be
      configured via the /api/config endpoint)
    - Yields control to the application
    - On shutdown: closes database connections cleanly
    """
    await init_db()
    # Create importer instance; token will be settable via /api/config
    app.state.importer = MatterhornUniversalImporter()
    yield
    # Cleanup on shutdown
    await app.state.importer.close()
    await close_connection(None)  # noqa: B008


# ---------------------------------------------------------------------------
# FastAPI Application Assembly
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Omni-Core E-Commerce Middleware",
    version="1.0.0",
    description="E-Commerce import middleware with Matterhorn integration",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Helper: Get current settings from database
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


# ---------------------------------------------------------------------------
# HTTP Route Handlers
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def master_dashboard(request: Request):
    """Render the main dashboard HTML page with product statistics."""
    async with get_connection() as db:
        async with db.execute("SELECT COUNT(*) FROM products") as cursor:
            total_products: int = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM products WHERE status = 'active'") as cursor:
            active_products: int = (await cursor.fetchone())[0]

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "total_products": total_products, "active_products": active_products},
    )


@app.get("/admin/products", response_class=HTMLResponse)
async def products_page(request: Request):
    """Render the product catalog page."""
    async with get_connection() as db:
        async with db.execute("SELECT sku, title, category, supplier_price, stock FROM products") as cursor:
            products = await cursor.fetchall()
    return templates.TemplateResponse(
        "products.html",
        {"request": request, "products": products},
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
    """
    importer: MatterhornUniversalImporter = request.app.state.importer

    # Start the import task in the background
    background_tasks.add_task(importer.import_from_rest)

    return JSONResponse({
        "status": "started",
        "message": "Matterhorn import triggered in background",
        "methods": ["rest_api", "xml_offer"],
    })


@app.get("/api/health")
async def health_check():
    """Simple health check endpoint."""
    try:
        # Verify database connectivity
        async with get_connection() as db:
            await db.execute("SELECT 1")
        return JSONResponse({"status": "healthy", "database": "connected"})
    except Exception:
        return JSONResponse({"status": "unhealthy"}, status_code=500)


@app.get("/api/export/shopify.csv")
async def export_shopify_csv():
    """Generate and return Shopify-compatible CSV export."""
    async with get_connection() as db:
        async with db.execute("SELECT sku, title, supplier_price, stock FROM products") as cursor:
            rows = await cursor.fetchall()

    # Generate CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Handle", "Title", "Variant SKU", "Variant Price", "Inventory Qty"])
    for row in rows:
        sku, title, supplier_price, stock = row
        writer.writerow([sku, title, sku, supplier_price, stock])

    output.seek(0)
    return StreamingResponse(
        io.iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="shopify_products.csv"'},
    )


@app.get("/api/export/google-shopping.xml")
async def export_google_shopping_xml():
    """Generate and return Google Shopping XML feed."""
    async with get_connection() as db:
        async with db.execute("SELECT sku, title, supplier_price, status FROM products") as cursor:
            rows = await cursor.fetchall()

    # Generate XML
    output = io.StringIO()
    output.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    output.write("<rss xmlns:g=\"http://base.google.com/rss/2.0\" version=\"2.0\">\n")
    output.write("  <channel>\n")
    output.write("    <title>Omni-Core Produkt-Feed</title>\n")
    output.write("    <link>http://localhost:8000</link>\n")
    output.write("    <description>Produkt-Feed aus Omni-Core Middleware</description>\n")

    for row in rows:
        sku, title, price, status = row
        output.write("    <item>\n")
        output.write("      <title>{}</title>\n".format(title))
        output.write("      <g:product_id>{}</g:product_id>\n".format(sku))
        output.write("      <g:price>{}</g:price>\n".format(price))
        output.write("      <g:availability>{}</g:availability>\n".format("in stock" if status == "active" else "out of stock"))
        output.write("</item>\n")

    output.write("</channel>\n")
    output.write("</rss>")

    output.seek(0)
    return StreamingResponse(
        io.iter([output.getvalue()]),
        media_type="application/xml",
        headers={"Content-Disposition": 'attachment; filename="google-shopping.xml"'},
    )


# ---------------------------------------------------------------------------
# Development / Direct Execution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, loop="uvloop")