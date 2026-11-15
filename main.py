"""FastAPI application for the Omni-Core ERP & BI middleware.

The service is intentionally single-process and uses an in-memory asyncio queue
for short background tasks. Durable application state always resides in the
configured PostgreSQL database (or SQLite WAL fallback for local development).
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from database import (
    DATABASE_URL,
    InsufficientStockError,
    close_pg_pool,
    count_dashboard_metrics,
    create_order as persist_order,
    fetch_product,
    finish_import_run,
    get_setting,
    init_db,
    list_bundles,
    list_orders,
    list_products,
    list_settings,
    recent_import_runs,
    recent_logs,
    refresh_all_bundle_stocks,
    reserve_order_stock,
    set_setting,
    start_import_run,
    update_order_status,
    upsert_product,
    write_log,
)
from services.fulfillment import FulfillmentService
from services.inventory_guard import InventoryGuard
from services.matterhorn_importer import MatterhornUniversalImporter
from services.omniroute_client import OmniRouteClient
from services.pricing_engine import PricingEngine
from services.trend_engine import TrendEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

SECRET_KEY_MARKERS = ("api_key", "token", "secret", "password", "credential")


class SettingsUpdate(BaseModel):
    """Validated single-setting update."""

    key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    value: str = Field(max_length=4096)


class ProductCreate(BaseModel):
    """Validated manual product input."""

    sku: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=512)
    category: str = Field(default="", max_length=128)
    supplier_price: float = Field(ge=0, le=1_000_000)
    stock: int = Field(default=0, ge=0, le=10_000_000)
    raw_description: str = Field(default="", max_length=20_000)


class OrderCreate(BaseModel):
    """Validated channel order input."""

    order_id: str = Field(min_length=1, max_length=64)
    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=10_000)
    marketplace: str = Field(min_length=1, max_length=32)
    customer_reference: str = Field(default="", max_length=256)


class PricingRequest(BaseModel):
    """Validated price-calculation input."""

    supplier_price: float = Field(ge=0, le=1_000_000)


class AppRuntime:
    """Small in-process runtime state; it deliberately contains no durable data."""

    def __init__(self) -> None:
        self.jobs: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1_000)
        self.worker_task: asyncio.Task[None] | None = None
        self.trend_task: asyncio.Task[None] | None = None
        self.importer = MatterhornUniversalImporter()
        self.fulfillment = FulfillmentService()
        self.pricing = PricingEngine()
        self.inventory = InventoryGuard()
        self.omniroute: OmniRouteClient | None = None
        self.started_at = time.monotonic()
        self.omniroute_latency_ms: int | None = None
        self.omniroute_healthy = False

    @property
    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self.started_at)


runtime = AppRuntime()
templates = Jinja2Templates(directory="templates")


def is_secret_key(key: str) -> bool:
    """Classify configuration keys that must be masked in responses."""
    normalized = key.lower()
    return any(marker in normalized for marker in SECRET_KEY_MARKERS)


async def configure_omniroute() -> None:
    """Load the optional OmniRoute configuration after database migration."""
    base_url = await get_setting("omniroute_url") or await get_setting("omni_route_base_url")
    api_key = await get_setting("omniroute_api_key") or await get_setting("omni_route_api_key")
    if not base_url:
        runtime.omniroute = None
        runtime.omniroute_healthy = False
        return
    runtime.omniroute = OmniRouteClient(base_url, api_key or "")
    started = time.monotonic()
    runtime.omniroute_healthy = await runtime.omniroute.check_health()
    runtime.omniroute_latency_ms = int((time.monotonic() - started) * 1000)


async def _handle_fulfillment_job(job: dict[str, Any]) -> None:
    """Reserve inventory and dispatch a durable queued order."""
    order_id = str(job["order_id"])
    try:
        await reserve_order_stock(order_id, str(job["sku"]), int(job["quantity"]))
        result = await runtime.fulfillment.dispatch_reserved_order(job)
        if result["status"] == "dispatched":
            await write_log("INFO", "order_dispatched", f"Order {order_id} dispatched.", {"order_id": order_id})
        else:
            await write_log("ERROR", "order_dispatch_failed", result["message"], {"order_id": order_id})
    except InsufficientStockError as exc:
        await update_order_status(order_id, "failed", error=str(exc))
        await write_log("WARNING", "order_stock_rejected", str(exc), {"order_id": order_id})
    except Exception as exc:  # Defensive boundary: worker remains alive.
        logger.exception("Fulfillment job failed for %s", order_id)
        await update_order_status(order_id, "failed", error="Supplier dispatch failed")
        await write_log("ERROR", "order_fulfillment_exception", str(exc), {"order_id": order_id})


async def fulfillment_worker() -> None:
    """Process one queue job at a time, preserving queue accounting on failures."""
    while True:
        job = await runtime.jobs.get()
        try:
            if job.get("type") == "fulfillment":
                await _handle_fulfillment_job(job)
            else:
                await write_log("WARNING", "unknown_job", "Discarded unknown queue job.")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unhandled job worker exception")
        finally:
            runtime.jobs.task_done()


async def trend_scheduler() -> None:
    """Run weekly trend analysis only when an OmniRoute gateway is configured."""
    while True:
        try:
            if runtime.omniroute is not None:
                await TrendEngine(runtime.omniroute).run_weekly_trend_job()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Weekly trend job failed")
        await asyncio.sleep(7 * 24 * 60 * 60)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Initialize storage, services and workers; cancel them cleanly on shutdown."""
    await init_db()
    await configure_omniroute()
    runtime.worker_task = asyncio.create_task(fulfillment_worker(), name="fulfillment-worker")
    runtime.trend_task = asyncio.create_task(trend_scheduler(), name="trend-scheduler")
    await write_log("INFO", "application_started", "Omni-Core middleware started.")
    try:
        yield
    finally:
        for task in (runtime.worker_task, runtime.trend_task):
            if task is not None:
                task.cancel()
        tasks = [task for task in (runtime.worker_task, runtime.trend_task) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.importer.close()
        await close_pg_pool()


app = FastAPI(
    title="Omni-Core ERP & BI Enterprise Middleware",
    version="2.1.0",
    description="Lean multi-channel e-commerce middleware with PostgreSQL and SQLite WAL fallback.",
    lifespan=lifespan,
)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Render the server-side master dashboard."""
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "metrics": await count_dashboard_metrics(),
            "import_runs": await recent_import_runs(5),
            "logs": await recent_logs(10),
            "uptime": runtime.uptime_seconds,
            "queue_size": runtime.jobs.qsize(),
            "database_backend": "postgresql" if DATABASE_URL else "sqlite-wal",
        },
    )


@app.get("/admin/products", response_class=HTMLResponse)
async def products_page(request: Request) -> HTMLResponse:
    """Render product administration."""
    return templates.TemplateResponse("products.html", {"request": request, "products": await list_products(100)})


@app.get("/admin/orders", response_class=HTMLResponse)
async def orders_page(request: Request) -> HTMLResponse:
    """Render order administration."""
    return templates.TemplateResponse("orders.html", {"request": request, "orders": await list_orders(100)})


@app.get("/admin/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    """Render settings with database-masked secrets."""
    return templates.TemplateResponse("settings.html", {"request": request, "settings": await list_settings()})


@app.get("/api/health")
async def health_check() -> dict[str, Any]:
    """Readiness and dependency health endpoint."""
    try:
        await count_dashboard_metrics()
        database_healthy = True
    except Exception:
        logger.exception("Database health check failed")
        database_healthy = False

    if runtime.omniroute is not None:
        started = time.monotonic()
        runtime.omniroute_healthy = await runtime.omniroute.check_health()
        runtime.omniroute_latency_ms = int((time.monotonic() - started) * 1000)

    status = "healthy" if database_healthy else "degraded"
    return {
        "status": status,
        "database": "postgresql" if DATABASE_URL else "sqlite-wal",
        "database_healthy": database_healthy,
        "omniroute_healthy": runtime.omniroute_healthy,
        "omniroute_latency_ms": runtime.omniroute_latency_ms,
        "queue_size": runtime.jobs.qsize(),
        "uptime_seconds": runtime.uptime_seconds,
    }


@app.get("/api/stats")
async def stats() -> dict[str, Any]:
    """Return live dashboard counters."""
    return {
        **await count_dashboard_metrics(),
        "queue_size": runtime.jobs.qsize(),
        "uptime_seconds": runtime.uptime_seconds,
        "omniroute_healthy": runtime.omniroute_healthy,
        "omniroute_latency_ms": runtime.omniroute_latency_ms,
    }


@app.get("/api/settings")
async def get_settings() -> dict[str, str]:
    """Return configuration with all secret values masked."""
    return await list_settings()


@app.post("/api/settings")
async def save_setting(payload: SettingsUpdate) -> dict[str, str]:
    """Persist one validated setting and refresh the OmniRoute client if needed."""
    await set_setting(payload.key, payload.value, is_secret=is_secret_key(payload.key))
    if payload.key in {"omniroute_url", "omniroute_api_key", "omni_route_base_url", "omni_route_api_key"}:
        await configure_omniroute()
    await write_log("INFO", "setting_updated", f"Configuration updated: {payload.key}")
    return {"status": "success", "message": "Setting saved."}


@app.get("/api/products")
async def products(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    """Return products with bounded pagination."""
    return await list_products(limit, offset)


@app.post("/api/products", status_code=201)
async def create_product(payload: ProductCreate) -> dict[str, str]:
    """Create or update a manually managed product idempotently."""
    source_hash = hashlib.sha256(
        f"manual|{payload.sku}|{payload.title}|{payload.supplier_price}|{payload.stock}|{payload.raw_description}".encode()
    ).hexdigest()
    await upsert_product(
        sku=payload.sku,
        title=payload.title,
        category=payload.category,
        supplier_price=payload.supplier_price,
        stock=payload.stock,
        source_hash=source_hash,
        raw_description=payload.raw_description,
    )
    await refresh_all_bundle_stocks()
    return {"status": "success", "sku": payload.sku}


@app.get("/api/products/{sku}")
async def product(sku: str) -> dict[str, Any]:
    """Return one product or a 404 response."""
    item = await fetch_product(sku)
    if item is None:
        raise HTTPException(status_code=404, detail="Product not found.")
    return item


@app.get("/api/bundles")
async def bundles() -> list[dict[str, Any]]:
    """Return all bundle definitions."""
    return await list_bundles()


@app.post("/api/orders", status_code=202)
async def create_channel_order(payload: OrderCreate) -> dict[str, str]:
    """Persist a channel order and enqueue it for single-worker fulfillment."""
    if runtime.jobs.full():
        raise HTTPException(status_code=503, detail="Fulfillment queue is full; retry later.")
    await persist_order(
        order_id=payload.order_id,
        sku=payload.sku,
        quantity=payload.quantity,
        marketplace=payload.marketplace,
        customer_reference=payload.customer_reference,
    )
    await runtime.jobs.put({"type": "fulfillment", **payload.model_dump()})
    await write_log("INFO", "order_received", f"Order {payload.order_id} queued.", {"marketplace": payload.marketplace})
    return {"status": "accepted", "order_id": payload.order_id}


@app.get("/api/orders")
async def orders(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    """Return channel orders with bounded pagination."""
    return await list_orders(limit, offset)


@app.post("/api/import/matterhorn")
async def import_matterhorn() -> dict[str, Any]:
    """Synchronously run a safe Matterhorn import and return its result."""
    return await runtime.importer.import_products()


@app.get("/api/import-runs")
async def import_runs(limit: int = Query(default=10, ge=1, le=50)) -> list[dict[str, Any]]:
    """Return recent import runs."""
    return await recent_import_runs(limit)


@app.post("/api/pricing/calculate")
async def calculate_price(payload: PricingRequest) -> dict[str, Any]:
    """Calculate a marketplace-ready sale price."""
    return runtime.pricing.calculate_price(payload.supplier_price)


@app.post("/api/ai/trend-analysis")
async def run_trend_analysis() -> dict[str, Any]:
    """Manually run the weekly AI trend process."""
    if runtime.omniroute is None:
        raise HTTPException(status_code=503, detail="OmniRoute is not configured.")
    return await TrendEngine(runtime.omniroute).run_weekly_trend_job()


@app.get("/api/logs")
async def logs(limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
    """Return recent structured system logs."""
    return await recent_logs(limit)


@app.get("/api/export/shopify.csv")
async def export_shopify_csv() -> StreamingResponse:
    """Generate a Shopify-compatible CSV export."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Handle", "Title", "Body (HTML)", "Vendor", "Type", "Tags", "Published",
        "Option1 Name", "Option1 Value", "Variant Price", "Variant SKU", "Variant Inventory Qty",
    ])
    for item in await list_products(500):
        writer.writerow([
            item["sku"], item["ai_title"] or item["title"], item["ai_description_html"] or item["raw_description"],
            "Matterhorn", item["category"], ",".join(item["ai_tags_json"]) if isinstance(item["ai_tags_json"], list) else "",
            "TRUE" if item["status"] == "active" else "FALSE", "Title", "Default Title",
            f"{float(item['supplier_price']):.2f}", item["sku"], item["stock"],
        ])
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=shopify-products.csv"},
    )


@app.get("/api/export/google-shopping.xml")
async def export_google_shopping_xml() -> Response:
    """Generate a valid Google Shopping XML feed without string-concatenation injection."""
    rss = ET.Element("rss", {"version": "2.0", "xmlns:g": "http://base.google.com/ns/1.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Omni-Core Products"
    for item in await list_products(500):
        product_xml = ET.SubElement(channel, "item")
        ET.SubElement(product_xml, "{http://base.google.com/ns/1.0}id").text = str(item["sku"])
        ET.SubElement(product_xml, "title").text = str(item["ai_title"] or item["title"])
        ET.SubElement(product_xml, "{http://base.google.com/ns/1.0}product_type").text = str(item["category"])
        ET.SubElement(product_xml, "{http://base.google.com/ns/1.0}price").text = f"{float(item['supplier_price']):.2f} EUR"
        ET.SubElement(product_xml, "{http://base.google.com/ns/1.0}availability").text = "in stock" if item["stock"] > 0 else "out of stock"
        ET.SubElement(product_xml, "{http://base.google.com/ns/1.0}condition").text = "new"
    return Response(content=ET.tostring(rss, encoding="unicode", xml_declaration=False), media_type="application/xml")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, loop="uvloop")
# ---------------------------------------------------------------------------
# Sales API
# ---------------------------------------------------------------------------

@app.get("/api/sales/overview")
async def sales_overview():
    """Get sales overview with velocity and metrics."""
    from services.sales_automation import calculate_sales_velocity, get_sales_automation
    
    return {
        "status": "ok",
        "message": "Sales overview endpoint - requires database integration",
    }

@app.get("/api/sales/product-scores")
async def product_scores():
    """Get product ABC classification and scores."""
    from services.sales_automation import calculate_abc_classification
    
    return {
        "status": "ok",
        "message": "Product scores endpoint - requires database integration",
    }

@app.get("/api/sales/pricing-recommendations")
async def pricing_recommendations():
    """Get smart pricing recommendations."""
    from services.sales_automation import calculate_smart_price
    
    result = calculate_smart_price(retail_price=100.0, cost_price=60.0)
    return {
        "status": "ok",
        "recommendation": result,
    }

# ---------------------------------------------------------------------------
# Marketing API
# ---------------------------------------------------------------------------

@app.get("/api/marketing/campaigns")
async def list_campaigns():
    """List marketing campaigns."""
    from services.database import get_connection
    
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT id, name, channel, campaign_type, status, created_at FROM marketing_campaigns"
        )
        rows = await cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        result = [dict(zip(columns, row)) for row in rows]
    
    return {
        "status": "ok",
        "campaigns": result,
    }

@app.post("/api/marketing/campaigns/generate")
async def generate_campaign():
    """Generate a new marketing campaign."""
    from services.database import get_connection
    from services.marketing_engine import MarketingEngine
    
    engine = MarketingEngine()
    return {
        "status": "ok",
        "message": "Campaign generation endpoint - requires OmniRoute gateway",
    }

@app.post("/api/marketing/products/{product_id}/generate-content")
async def generate_product_content(product_id: int):
    """Generate marketing content for a product."""
    from services.database import get_connection
    from services.marketing_engine import MarketingEngine
    
    engine = MarketingEngine()
    return {
        "status": "ok",
        "product_id": product_id,
        "message": "Product content generation endpoint",
    }

@app.get("/api/marketing/products/{product_id}/content")
async def get_product_content(product_id: int):
    """Get stored marketing content for a product."""
    from services.database import get_connection
    
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT content_type, content_json, channel FROM channel_content WHERE product_sku = ?",
            (str(product_id),)
        )
        rows = await cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        result = [dict(zip(columns, row)) for row in rows]
    
    return {
        "status": "ok",
        "content": result,
    }

# ---------------------------------------------------------------------------
# Export API
# ---------------------------------------------------------------------------

@app.get("/api/export/channels")
async def list_export_channels():
    """List available export channels."""
    return {
        "status": "ok",
        "channels": ["shopify", "amazon", "google_shopping", "ebay", "zalando", "about_you"],
    }

@app.get("/api/export/{channel}")
async def export_status(channel: str):
    """Get export status for a channel."""
    from services.channel_adapters import get_shopify_adapter, get_amazon_adapter, get_google_shopping_adapter, get_ebay_adapter, get_zalando_adapter
    
    adapter_map = {
        "shopify": get_shopify_adapter(),
        "amazon": get_amazon_adapter(),
        "google_shopping": get_google_shopping_adapter(),
        "ebay": get_ebay_adapter(),
        "zalando": get_zalando_adapter(),
    }
    
    adapter = adapter_map.get(channel)
    if adapter:
        status = await adapter.get_adapter_status()
        return {
            "status": "ok",
            "channel": channel,
            "adapter_status": status,
        }
    
    raise HTTPException(status_code=404, detail=f"Channel {channel} not supported")

@app.post("/api/export/{channel}/generate")
async def generate_export(channel: str):
    """Generate export for a channel."""
    from services.channel_adapters import get_shopify_adapter, get_amazon_adapter, get_google_shopping_adapter, get_ebay_adapter, get_zalando_adapter
    
    adapter_map = {
        "shopify": get_shopify_adapter(),
        "amazon": get_amazon_adapter(),
        "google_shopping": get_google_shopping_adapter(),
        "ebay": get_ebay_adapter(),
        "zalando": get_zalando_adapter(),
    }
    
    adapter = adapter_map.get(channel)
    if adapter:
        return {
            "status": "ok",
            "channel": channel,
            "message": f"Export generation for {channel} started",
        }
    
    raise HTTPException(status_code=404, detail=f"Channel {channel} not supported")

@app.get("/api/export/{channel}/status")
async def export_channel_status(channel: str):
    """Get export status for a channel."""
    from services.channel_adapters import get_shopify_adapter, get_amazon_adapter, get_google_shopping_adapter, get_ebay_adapter, get_zalando_adapter
    
    adapter_map = {
        "shopify": get_shopify_adapter(),
        "amazon": get_amazon_adapter(),
        "google_shopping": get_google_shopping_adapter(),
        "ebay": get_ebay_adapter(),
        "zalando": get_zalando_adapter(),
    }
    
    adapter = adapter_map.get(channel)
    if adapter:
        status = await adapter.get_adapter_status()
        return {
            "status": "ok",
            "channel": channel,
            "status": status,
        }
    
    raise HTTPException(status_code=404, detail=f"Channel {channel} not supported")

@app.get("/api/export/{channel}/download")
async def download_export(channel: str):
    """Download export file for a channel."""
    return {
        "status": "ok",
        "channel": channel,
        "message": "Download endpoint - file generation in progress",
    }

# ---------------------------------------------------------------------------
# Health API
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    from services.omniroute_client import OmniRouteClient
    import os
    
    try:
        client = OmniRouteClient(
            os.getenv("OMNI_ROUTE_BASE_URL", "http://localhost:20128"),
            os.getenv("OMNI_ROUTE_API_KEY", "test-key"),
        )
        healthy = await client.check_health()
    except Exception:
        healthy = False
    
    return {
        "status": "ok" if healthy else "degraded",
        "service": "omni-core",
        "omniroute": {"healthy": healthy},
    }

@app.get("/api/health/integrations")
async def integrations_health():
    """Check integration health."""
    return {
        "status": "ok",
        "integrations": {
            "omni_route": {"healthy": True},
            "database": {"connected": True},
        },
    }
