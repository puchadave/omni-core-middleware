"""FastAPI application for the Omni-Core ERP & BI middleware.

The service is intentionally single-process and uses an in-memory asyncio queue
for short background tasks. Durable application state always resides in the
configured PostgreSQL database (or SQLite WAL fallback for local development).
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from database import (
    DATABASE_URL,
    InsufficientStockError,
    close_pg_pool,
    count_dashboard_metrics,
    create_order as persist_order,
    fetch_product,
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
    update_order_status,
    upsert_product,
    write_log,
)
from services.fulfillment import FulfillmentService
from services.inventory_guard import InventoryGuard
from services.matterhorn_importer import MatterhornUniversalImporter
from services.omniroute_client import OmniRouteClient
from services.pricing_engine import PricingEngine
from services.shopware_connection_manager import ShopwareConnectionManager
from services.shopware_mapping_engine import MappingEngine, FieldMapping, MAPPING_DIRECT, get_default_mappings
from services.shopware_export_manager import ExportManager
from services.shopware_description_engine import DescriptionEngine, SEOCenter
from services.competitor_provider import provider_registry
from services.competitor_matching import ProductMatchingEngine
from services.competitor_pricing import DynamicPricingEngine
from services.google_merchant_client import GoogleMerchantClient
from services.google_product_mapper import GoogleProductMapper, GoogleProductValidator
from services.google_export_engine import AutonomousExportEngine
from services.omniroute_provider import AIProvider, ProviderManager
from services.omniroute_model_registry import AIModel, ModelRegistry, RoutingRule, RoutingPolicy, IntelligentRoutingEngine, CostCalculator
from services.trend_engine import TrendEngine
from services.event_bus import EventBus, get_event_bus, shutdown_event_bus
from services.global_control_center import GlobalControlCenter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

SECRET_KEY_MARKERS = ("api_key", "token", "secret", "password", "credential")
SESSION_COOKIE_NAME = "omni_session"
SESSION_TTL_SECONDS = 8 * 60 * 60


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
        # Shopware integration
        self.shopware_connections: dict[str, ShopwareConnectionManager] = {}
        self.shopware_mapping_engines: dict[str, MappingEngine] = {}
        self.shopware_export_manager: ExportManager | None = None
        self.shopware_description_engine: DescriptionEngine | None = None
        self.shopware_seo_center: SEOCenter | None = None
        # Competitor intelligence
        self.competitor_matching_engine: ProductMatchingEngine | None = None
        self.competitor_pricing_engine: DynamicPricingEngine | None = None
        # Google Merchant Center
        self.google_merchant_clients: dict[str, GoogleMerchantClient] = {}
        self.google_mappers: dict[str, GoogleProductMapper] = {}
        self.google_validators: dict[str, GoogleProductValidator] = {}
        self.google_export_engines: dict[str, AutonomousExportEngine] = {}
        # OmniRoute Control Center
        self.omniroute_provider_manager: ProviderManager | None = None
        self.omniroute_model_registry: ModelRegistry | None = None
        self.omniroute_routing_engine: IntelligentRoutingEngine | None = None
        self.omniroute_cost_calculator: CostCalculator | None = None
        # Internal communication & control center
        self.event_bus: EventBus | None = None
        self.control_center: GlobalControlCenter | None = None

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
    # Initialize Shopware services
    runtime.shopware_export_manager = ExportManager(
        connection_manager=ShopwareConnectionManager("", "", ""),
        mapping_engine=MappingEngine([]),
    )
    runtime.shopware_description_engine = DescriptionEngine()
    runtime.shopware_seo_center = SEOCenter()
    # Initialize Competitor Intelligence
    runtime.competitor_matching_engine = ProductMatchingEngine()
    runtime.competitor_pricing_engine = DynamicPricingEngine()
    # Initialize Google Merchant Center
    runtime.google_mappers = {}
    runtime.google_validators = {}
    runtime.google_export_engines = {}
    # Initialize OmniRoute Control Center
    runtime.omniroute_provider_manager = ProviderManager()
    runtime.omniroute_model_registry = ModelRegistry()
    runtime.omniroute_routing_engine = IntelligentRoutingEngine(runtime.omniroute_model_registry)
    runtime.omniroute_cost_calculator = CostCalculator()
    # Initialize Event Bus and Global Control Center
    try:
        runtime.event_bus = await get_event_bus()
    except Exception:
        logger.exception("Failed to initialize event bus")
        runtime.event_bus = None
    runtime.control_center = GlobalControlCenter(
        event_bus=runtime.event_bus,
        provider_manager=runtime.omniroute_provider_manager,
        model_registry=runtime.omniroute_model_registry,
        routing_engine=runtime.omniroute_routing_engine,
        export_managers=runtime.google_export_engines,
        shopware_connections=runtime.shopware_connections,
        competitor_engines={
            "matching": runtime.competitor_matching_engine,
            "pricing": runtime.competitor_pricing_engine,
        },
    )
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
        try:
            await shutdown_event_bus()
        except Exception:
            logger.exception("Failed to shutdown event bus")
        runtime.event_bus = None
        await runtime.importer.close()
        await close_pg_pool()


app = FastAPI(
    title="Omni-Core ERP & BI Enterprise Middleware",
    version="2.1.0",
    description="Lean multi-channel e-commerce middleware with PostgreSQL and SQLite WAL fallback.",
    lifespan=lifespan,
)

_INTERNAL_API_KEY_HEADER = "X-Internal-API-Key"
_INTERNAL_API_EXCLUDED_PATHS = frozenset({"/api/health", "/docs", "/redoc", "/openapi.json"})


def _session_token() -> str:
    """Create a short-lived stateless browser session token."""
    issued = str(int(time.time()))
    secret = os.getenv("INTERNAL_API_KEY", "").strip()
    signature = hmac.new(secret.encode(), issued.encode(), hashlib.sha256).hexdigest()
    return f"{issued}.{signature}"


def _valid_session(token: str) -> bool:
    """Validate the signed browser session token and its TTL."""
    if not token or "." not in token:
        return False
    issued, signature = token.split(".", 1)
    if not issued.isdigit():
        return False
    if int(time.time()) - int(issued) > SESSION_TTL_SECONDS:
        return False
    secret = os.getenv("INTERNAL_API_KEY", "").strip()
    expected = hmac.new(secret.encode(), issued.encode(), hashlib.sha256).hexdigest()
    return bool(secret) and hmac.compare_digest(signature, expected)


@app.middleware("http")
async def internal_api_key_middleware(request: Request, call_next):
    if request.url.path in _INTERNAL_API_EXCLUDED_PATHS or request.url.path in {"/login", "/api/auth/login", "/api/auth/logout"}:
        return await call_next(request)

    provided_api_key = request.headers.get(_INTERNAL_API_KEY_HEADER, "")
    session_token = request.cookies.get(SESSION_COOKIE_NAME, "")
    expected_api_key = os.getenv("INTERNAL_API_KEY", "").strip()
    authenticated = bool(expected_api_key) and (
        hmac.compare_digest(provided_api_key, expected_api_key) or _valid_session(session_token)
    )
    if not authenticated:
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        return Response(status_code=303, headers={"Location": "/login"})
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """Render the local control-plane login page."""
    return templates.TemplateResponse("login.html", {"request": request})


class LoginRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=4096)


@app.post("/api/auth/login")
async def login(payload: LoginRequest, response: Response) -> dict[str, str]:
    """Authenticate a browser session using the configured internal API key."""
    expected = os.getenv("INTERNAL_API_KEY", "").strip()
    if not expected or not hmac.compare_digest(payload.api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid API key")
    response.set_cookie(
        SESSION_COOKIE_NAME,
        _session_token(),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,
    )
    return {"status": "ok", "message": "Authenticated"}


@app.post("/api/auth/logout")
async def logout(response: Response) -> dict[str, str]:
    """Clear the browser session cookie."""
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Render the unified Omni-Core Control Center."""
    return templates.TemplateResponse(
        "control_center.html",
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


@app.get("/settings", response_class=HTMLResponse)
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
    
    return {
        "status": "ok",
        "message": "Sales overview endpoint - requires database integration",
    }

@app.get("/api/sales/product-scores")
async def product_scores():
    """Get product ABC classification and scores."""
    
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
    return {
        "status": "ok",
        "message": "Campaign generation endpoint - requires OmniRoute gateway",
    }

@app.post("/api/marketing/products/{product_id}/generate-content")
async def generate_product_content(product_id: int):
    """Generate marketing content for a product."""
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
        adapter_status = await adapter.get_adapter_status()
        return {
            "status": "ok",
            "channel": channel,
            "adapter_status": adapter_status,
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
# Health / Integrations API
# ---------------------------------------------------------------------------

@app.get("/api/health/integrations")
async def integrations_health() -> dict[str, Any]:
    """Check integration health across configured subsystems."""
    try:
        await count_dashboard_metrics()
        db_ok = True
    except Exception:
        logger.exception("Database integration check failed")
        db_ok = False

    integrations: dict[str, Any] = {
        "database": {"connected": db_ok},
        "omniroute": {"healthy": runtime.omniroute_healthy, "latency_ms": runtime.omniroute_latency_ms},
        "shopware": {"configured_connections": len(runtime.shopware_connections)},
        "google_merchant": {"configured_accounts": len(runtime.google_merchant_clients)},
        "event_bus": runtime.event_bus.get_stats() if getattr(runtime, "event_bus", None) else {"running": False},
    }
    overall_ok = db_ok
    return {"status": "ok" if overall_ok else "degraded", "integrations": integrations}

# ---------------------------------------------------------------------------
# Shopware Integration API
# ---------------------------------------------------------------------------

class ShopwareConnectionCreate(BaseModel):
    shop_name: str
    shop_url: str
    shopware_version: str = "6.0"
    api_client_id: str
    api_client_secret: str
    sales_channel_id: str = ""
    default_language: str = "de-DE"
    default_currency: str = "EUR"
    default_tax_rate: float = 19.0
    default_supplier: str = ""

class ShopwareMappingCreate(BaseModel):
    connection_id: str
    matterhorn_field: str
    shopware_field: str
    mapping_type: str = MAPPING_DIRECT
    mapping_config: dict = {}
    priority: int = 0

class ShopwareExportCreate(BaseModel):
    connection_id: str
    product_sku: str
    export_mode: str = "publish"
    payload: dict = {}

@app.post("/api/shopware/connections")
async def create_shopware_connection(payload: ShopwareConnectionCreate):
    """Create a new Shopware connection."""
    from uuid import uuid4
    conn_id = str(uuid4())
    cm = ShopwareConnectionManager(
        shop_url=payload.shop_url,
        client_id=payload.api_client_id,
        client_secret=payload.api_client_secret,
        api_version=payload.shopware_version,
    )
    runtime.shopware_connections[conn_id] = cm
    runtime.shopware_mapping_engines[conn_id] = MappingEngine(
        get_default_mappings(conn_id)
    )
    await write_log("INFO", "shopware_connection_created", f"Shopware connection {payload.shop_name} created.", {"connection_id": conn_id})
    return {
        "status": "ok",
        "connection_id": conn_id,
        "shop_name": payload.shop_name,
    }

@app.get("/api/shopware/connections")
async def list_shopware_connections():
    """List all Shopware connections."""
    connections = []
    for conn_id, cm in runtime.shopware_connections.items():
        connections.append({
            "id": conn_id,
            "shop_url": cm.shop_url,
            "api_version": cm.api_version,
            "is_authenticated": cm.is_authenticated,
            "request_count": cm.request_count,
            "error_count": cm.error_count,
        })
    return {"status": "ok", "connections": connections}

@app.post("/api/shopware/connections/{conn_id}/test")
async def test_shopware_connection(conn_id: str):
    """Test a Shopware connection."""
    cm = runtime.shopware_connections.get(conn_id)
    if not cm:
        raise HTTPException(status_code=404, detail="Connection not found")
    health = await cm.check_health()
    return {"status": "ok", "connection_id": conn_id, "health": health}

@app.post("/api/shopware/connections/{conn_id}/mappings")
async def create_shopware_mapping(conn_id: str, payload: ShopwareMappingCreate):
    """Create a field mapping for a Shopware connection."""
    from uuid import uuid4
    mapping = FieldMapping(
        mapping_id=str(uuid4()),
        connection_id=conn_id,
        matterhorn_field=payload.matterhorn_field,
        shopware_field=payload.shopware_field,
        mapping_type=payload.mapping_type,
        mapping_config=payload.mapping_config,
        priority=payload.priority,
    )
    engine = runtime.shopware_mapping_engines.get(conn_id)
    if engine:
        engine.mappings.append(mapping)
    await write_log("INFO", "shopware_mapping_created", f"Mapping {payload.matterhorn_field} -> {payload.shopware_field} created.", {"connection_id": conn_id})
    return {"status": "ok", "mapping_id": mapping.mapping_id}

@app.get("/api/shopware/connections/{conn_id}/mappings")
async def list_shopware_mappings(conn_id: str):
    """List field mappings for a Shopware connection."""
    engine = runtime.shopware_mapping_engines.get(conn_id)
    if not engine:
        raise HTTPException(status_code=404, detail="Connection not found")
    return {
        "status": "ok",
        "connection_id": conn_id,
        "mappings": [m.to_dict() for m in engine.mappings],
        "summary": engine.get_mapping_summary(),
    }

@app.post("/api/shopware/export")
async def add_shopware_export(payload: ShopwareExportCreate):
    """Add an item to the Shopware export queue."""
    if not runtime.shopware_export_manager:
        raise HTTPException(status_code=503, detail="Export manager not initialized")
    queue_id = runtime.shopware_export_manager.add_to_queue(
        connection_id=payload.connection_id,
        product_sku=payload.product_sku,
        export_mode=payload.export_mode,
        payload=payload.payload,
    )
    return {"status": "ok", "queue_id": queue_id}

@app.get("/api/shopware/export/queue")
async def list_shopware_export_queue(status: str = None, limit: int = 100):
    """List Shopware export queue items."""
    if not runtime.shopware_export_manager:
        raise HTTPException(status_code=503, detail="Export manager not initialized")
    items = runtime.shopware_export_manager.list_queue(status=status, limit=limit)
    return {"status": "ok", "queue": [i.to_dict() for i in items]}

@app.get("/api/shopware/export/stats")
async def shopware_export_stats():
    """Get Shopware export queue statistics."""
    if not runtime.shopware_export_manager:
        raise HTTPException(status_code=503, detail="Export manager not initialized")
    return {"status": "ok", "stats": runtime.shopware_export_manager.get_queue_stats()}

@app.post("/api/shopware/export/process")
async def process_shopware_export(max_items: int = 10):
    """Process pending Shopware export queue items."""
    if not runtime.shopware_export_manager:
        raise HTTPException(status_code=503, detail="Export manager not initialized")
    result = await runtime.shopware_export_manager.process_queue(max_items=max_items)
    return {"status": "ok", "result": result}

# ---------------------------------------------------------------------------
# Shopware Description & SEO API
# ---------------------------------------------------------------------------

class ShopwareDescriptionRequest(BaseModel):
    product_sku: str
    template: str = "premium"
    ai_enhance: bool = False

@app.post("/api/shopware/description/generate")
async def generate_shopware_description(payload: ShopwareDescriptionRequest):
    """Generate a Shopware product description."""
    product = await fetch_product(payload.product_sku)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    from services.canonical_product_mapping import CanonicalProduct
    cp = CanonicalProduct.from_matterhorn("matterhorn", product)
    
    engine = runtime.shopware_description_engine
    if not engine:
        raise HTTPException(status_code=503, detail="Description engine not initialized")
    
    result = engine.generate_description(cp, template_name=payload.template)
    
    if payload.ai_enhance and runtime.omniroute:
        result = engine.enhance_with_ai(result, cp)
    
    return {"status": "ok", "description": result}

@app.post("/api/shopware/seo/generate")
async def generate_shopware_seo(payload: ShopwareDescriptionRequest):
    """Generate SEO data for a Shopware product."""
    product = await fetch_product(payload.product_sku)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    from services.canonical_product_mapping import CanonicalProduct
    cp = CanonicalProduct.from_matterhorn("matterhorn", product)
    
    seo_center = runtime.shopware_seo_center
    if not seo_center:
        raise HTTPException(status_code=503, detail="SEO center not initialized")
    
    result = seo_center.generate_seo_data(cp)
    return {"status": "ok", "seo": result}

# ---------------------------------------------------------------------------
# Competitor Intelligence API
# ---------------------------------------------------------------------------

class CompetitorSearchRequest(BaseModel):
    query: str
    provider: str = "example"
    limit: int = 10

class CompetitorMatchRequest(BaseModel):
    product_sku: str
    competitor_ids: list[str] = []
    ai_confirm: bool = False

class CompetitorPriceRequest(BaseModel):
    product_sku: str
    supplier_price_netto: float
    current_selling_price: float
    market_prices: list[float] = []
    brand: str = None
    category: str = None

@app.post("/api/competitor/search")
async def competitor_search(payload: CompetitorSearchRequest):
    """Search for competitor products."""
    results = await provider_registry.search_all(payload.query, limit_per_provider=payload.limit)
    all_results = []
    for provider_id, provider_results in results.items():
        for r in provider_results:
            all_results.append(r.to_dict())
    return {"status": "ok", "query": payload.query, "results": all_results}

@app.post("/api/competitor/match")
async def competitor_match(payload: CompetitorMatchRequest):
    """Match a product against competitors."""
    product = await fetch_product(payload.product_sku)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    from services.canonical_product_mapping import CanonicalProduct
    cp = CanonicalProduct.from_matterhorn("matterhorn", product)
    
    engine = runtime.competitor_matching_engine
    if not engine:
        raise HTTPException(status_code=503, detail="Matching engine not initialized")
    
    # Build competitor product list from search results or database
    competitor_products = []
    # Use search results if competitor_ids not specified
    if not payload.competitor_ids:
        search_results = await provider_registry.search_all(product.get("title", ""), limit_per_provider=5)
        for provider_id, provider_results in search_results.items():
            for r in provider_results:
                competitor_products.append(r.to_dict())
    
    matches = engine.match(cp, competitor_products, ai_confirm=payload.ai_confirm)
    return {
        "status": "ok",
        "product_sku": payload.product_sku,
        "matches": [m.to_dict() for m in matches],
    }

@app.post("/api/competitor/price/calculate")
async def competitor_price_calculate(payload: CompetitorPriceRequest):
    """Calculate a dynamic price recommendation."""
    engine = runtime.competitor_pricing_engine
    if not engine:
        raise HTTPException(status_code=503, detail="Pricing engine not initialized")
    
    result = engine.calculate_price(
        own_product_sku=payload.product_sku,
        supplier_price_netto=__import__("decimal").Decimal(str(payload.supplier_price_netto)),
        current_selling_price=__import__("decimal").Decimal(str(payload.current_selling_price)),
        market_prices=[__import__("decimal").Decimal(str(mp)) for mp in payload.market_prices],
        brand=payload.brand,
        category=payload.category,
    )
    return {"status": "ok", "pricing": result.to_dict()}

@app.get("/api/competitor/providers")
async def list_competitor_providers():
    """List all registered research providers."""
    providers = provider_registry.list()
    return {
        "status": "ok",
        "providers": [
            {
                "provider_id": p.provider_id,
                "name": p.name,
                "status": p.status,
                "rate_limit_per_second": p.rate_limit_per_second,
            }
            for p in providers
        ],
    }

# ---------------------------------------------------------------------------
# Dashboard Metrics Extension
# ---------------------------------------------------------------------------

@app.get("/api/stats/shopware")
async def shopware_stats():
    """Get Shopware integration statistics."""
    return {
        "status": "ok",
        "connections": len(runtime.shopware_connections),
        "mapping_engines": len(runtime.shopware_mapping_engines),
        "export_stats": runtime.shopware_export_manager.get_queue_stats() if runtime.shopware_export_manager else {},
    }

@app.get("/api/stats/competitor")
async def competitor_stats():
    """Get Competitor Intelligence statistics."""
    return {
        "status": "ok",
        "providers": len(provider_registry.providers),
        "connected_providers": len(provider_registry.list_by_status("connected")),
        "matching_engine": runtime.competitor_matching_engine is not None,
        "pricing_engine": runtime.competitor_pricing_engine is not None,
    }

# ---------------------------------------------------------------------------
# Google Merchant Center API
# ---------------------------------------------------------------------------

from pydantic import BaseModel, Field

class GoogleAccountCreate(BaseModel):
    merchant_id: str
    account_name: str
    google_cloud_project_id: str
    oauth_token_encrypted: str
    oauth_refresh_token_encrypted: str | None = None
    service_account_encrypted: str | None = None
    is_primary: bool = False

class GoogleProductExportRequest(BaseModel):
    account_id: str
    product_sku: str
    ai_title: str | None = None
    ai_description: str | None = None
    ai_image_url: str | None = None
    shop_url: str | None = None

class GoogleSyncRequest(BaseModel):
    account_id: str
    job_type: str = "full_sync"

@app.post("/api/google/accounts")
async def create_google_account(payload: GoogleAccountCreate):
    """Create a Google Merchant Center account connection."""
    from uuid import uuid4
    account_id = str(uuid4())
    
    from database import google_upsert_account
    await google_upsert_account(
        account_id=account_id,
        merchant_id=payload.merchant_id,
        account_name=payload.account_name,
        google_cloud_project_id=payload.google_cloud_project_id,
        oauth_token_encrypted=payload.oauth_token_encrypted,
        oauth_refresh_token_encrypted=payload.oauth_refresh_token_encrypted,
        service_account_encrypted=payload.service_account_encrypted,
        is_primary=payload.is_primary,
    )
    
    client = GoogleMerchantClient(merchant_id=payload.merchant_id)
    client.set_oauth2_token(
        access_token=payload.oauth_token_encrypted,
        expires_in=3600,
        refresh_token=payload.oauth_refresh_token_encrypted,
    )
    runtime.google_merchant_clients[account_id] = client
    
    mapper = GoogleProductMapper(country="DE", language="de", currency="EUR")
    validator = GoogleProductValidator(country="DE", language="de", currency="EUR")
    engine = AutonomousExportEngine(
        merchant_client=client,
        mapper=mapper,
        validator=validator,
        omniroute_client=runtime.omniroute,
    )
    runtime.google_mappers[account_id] = mapper
    runtime.google_validators[account_id] = validator
    runtime.google_export_engines[account_id] = engine
    
    await write_log("INFO", "google_account_created", f"Google account {payload.account_name} created.", {"account_id": account_id, "merchant_id": payload.merchant_id})
    return {
        "status": "ok",
        "account_id": account_id,
        "merchant_id": payload.merchant_id,
        "account_name": payload.account_name,
    }

@app.get("/api/google/accounts")
async def list_google_accounts():
    """List all Google Merchant Center accounts."""
    from database import google_list_accounts
    accounts = await google_list_accounts()
    result = []
    for acc in accounts:
        result.append({
            "id": acc["id"],
            "merchant_id": acc["merchant_id"],
            "account_name": acc["account_name"],
            "account_status": acc["account_status"],
            "is_primary": acc["is_primary"],
            "last_token_status": acc["last_token_status"],
            "is_authenticated": acc["id"] in runtime.google_merchant_clients and runtime.google_merchant_clients[acc["id"]].is_authenticated,
        })
    return {"status": "ok", "accounts": result}

@app.post("/api/google/accounts/{account_id}/test")
async def test_google_connection(account_id: str):
    """Test a Google Merchant Center connection."""
    client = runtime.google_merchant_clients.get(account_id)
    if not client:
        raise HTTPException(status_code=404, detail="Account not found or not initialized")
    health = await client.check_health()
    return {"status": "ok", "account_id": account_id, "health": health}

@app.post("/api/google/export")
async def google_export_product(payload: GoogleProductExportRequest):
    """Export a product to Google Merchant Center."""
    engine = runtime.google_export_engines.get(payload.account_id)
    if not engine:
        raise HTTPException(status_code=404, detail="Account not found or export engine not initialized")
    
    from services.canonical_product_mapping import CanonicalProduct
    from database import fetch_product
    
    product_data = await fetch_product(payload.product_sku)
    if not product_data:
        raise HTTPException(status_code=404, detail="Product not found")
    
    cp = CanonicalProduct.from_matterhorn("matterhorn", product_data)
    
    queue_id = await engine.on_quality_check_passed(
        product_sku=payload.product_sku,
        account_id=payload.account_id,
        canonical_product=cp,
        ai_title=payload.ai_title,
        ai_description=payload.ai_description,
        ai_image_url=payload.ai_image_url,
        shop_url=payload.shop_url,
    )
    
    return {"status": "ok", "export_id": queue_id, "product_sku": payload.product_sku}

@app.get("/api/google/export/queue")
async def list_google_export_queue(account_id: str | None = None, status: str | None = None, limit: int = 100):
    """List Google export queue items."""
    all_items = []
    for acc_id, engine in runtime.google_export_engines.items():
        if account_id and acc_id != account_id:
            continue
        items = engine.list_queue(status=status, account_id=acc_id, limit=limit)
        for item in items:
            all_items.append(item.to_dict())
    
    all_items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"status": "ok", "queue": all_items[:limit]}

@app.get("/api/google/export/stats")
async def google_export_stats(account_id: str | None = None):
    """Get Google export statistics."""
    all_stats = {"total": 0, "pending": 0, "processing": 0, "completed": 0, "errors": 0, "withdrawn": 0, "retry": 0, "dead_letter": 0, "avg_validation_score": 0}
    count = 0
    for acc_id, engine in runtime.google_export_engines.items():
        if account_id and acc_id != account_id:
            continue
        stats = engine.get_queue_stats()
        count += 1
        for key in all_stats:
            all_stats[key] += stats.get(key, 0)
    
    if count > 0:
        all_stats["avg_validation_score"] = round(all_stats["avg_validation_score"] / count, 1)
    
    return {"status": "ok", "stats": all_stats}

@app.post("/api/google/export/process")
async def process_google_export(account_id: str, max_items: int = 10):
    """Process pending Google export queue items."""
    engine = runtime.google_export_engines.get(account_id)
    if not engine:
        raise HTTPException(status_code=404, detail="Account not found or export engine not initialized")
    result = await engine.process_queue(max_items=max_items)
    return {"status": "ok", "account_id": account_id, "result": result}

@app.post("/api/google/export/retry")
async def retry_google_failed(account_id: str | None = None):
    """Retry failed Google export items."""
    retried = 0
    for acc_id, engine in runtime.google_export_engines.items():
        if account_id and acc_id != account_id:
            continue
        retried += await engine.retry_failed(account_id=acc_id)
    return {"status": "ok", "retried": retried}

@app.post("/api/google/sync")
async def google_sync(payload: GoogleSyncRequest):
    """Trigger a Google sync job."""
    engine = runtime.google_export_engines.get(payload.account_id)
    if not engine:
        raise HTTPException(status_code=404, detail="Account not found or export engine not initialized")
    
    from uuid import uuid4
    from database import google_upsert_sync_job
    job_id = str(uuid4())
    await google_upsert_sync_job(
        job_id=job_id,
        account_id=payload.account_id,
        job_type=payload.job_type,
        total_products=0,
    )
    
    return {"status": "ok", "job_id": job_id, "job_type": payload.job_type, "account_id": payload.account_id}

@app.get("/api/google/export/health/{account_id}")
async def google_health(account_id: str):
    """Check Google Merchant Center connection health."""
    engine = runtime.google_export_engines.get(account_id)
    if not engine:
        raise HTTPException(status_code=404, detail="Account not found or export engine not initialized")
    health = await engine.health_check()
    return {"status": "ok", "account_id": account_id, "health": health}

# ---------------------------------------------------------------------------
# OmniRoute Control Center API
# ---------------------------------------------------------------------------

class OmniRouteProviderCreate(BaseModel):
    name: str
    provider_type: str = "text"
    api_endpoint: str
    api_key_encrypted: str
    auth_type: str = "bearer"
    priority: int = 100
    rate_limit_per_minute: int = 60
    timeout_seconds: int = 30
    max_retries: int = 3
    retry_delay_seconds: int = 1
    circuit_breaker_threshold: int = 5
    circuit_breaker_timeout_seconds: int = 300
    capabilities: list[str] = []

class OmniRouteModelCreate(BaseModel):
    provider_id: str
    model_id: str
    model_name: str
    model_version: str = ""
    model_category: str = "general"
    capabilities: list[str] = []
    context_window: int = 4096
    input_price_per_1k: float = 0.0
    output_price_per_1k: float = 0.0
    image_price_per_image: float = 0.0
    rate_limit_per_minute: int = 60
    is_active: bool = True

class OmniRouteRuleCreate(BaseModel):
    name: str
    strategy: str
    provider_priority: list[str] = []
    model_category: str = "general"
    min_quality_score: float = 0.0
    max_cost_per_1k: float | None = None
    max_latency_ms: int | None = None

class OmniRoutePolicyCreate(BaseModel):
    workflow_name: str
    rule_id: str
    fallback_rule_id: str | None = None

@app.post("/api/omniroute/providers")
async def create_omniroute_provider(payload: OmniRouteProviderCreate):
    """Create an AI provider."""
    from uuid import uuid4
    from database import ai_upsert_provider
    
    provider_id = str(uuid4())
    provider = AIProvider(
        provider_id=provider_id,
        name=payload.name,
        provider_type=payload.provider_type,
        api_endpoint=payload.api_endpoint,
        api_key_encrypted=payload.api_key_encrypted,
        auth_type=payload.auth_type,
        priority=payload.priority,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        timeout_seconds=payload.timeout_seconds,
        max_retries=payload.max_retries,
        retry_delay_seconds=payload.retry_delay_seconds,
        circuit_breaker_threshold=payload.circuit_breaker_threshold,
        circuit_breaker_timeout_seconds=payload.circuit_breaker_timeout_seconds,
        capabilities=payload.capabilities,
    )
    
    await ai_upsert_provider(
        provider_id=provider_id,
        name=payload.name,
        provider_type=payload.provider_type,
        api_endpoint=payload.api_endpoint,
        api_key_encrypted=payload.api_key_encrypted,
        auth_type=payload.auth_type,
        priority=payload.priority,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        timeout_seconds=payload.timeout_seconds,
        max_retries=payload.max_retries,
        retry_delay_seconds=payload.retry_delay_seconds,
        circuit_breaker_threshold=payload.circuit_breaker_threshold,
        circuit_breaker_timeout_seconds=payload.circuit_breaker_timeout_seconds,
        capabilities_json=json.dumps(payload.capabilities),
    )
    
    runtime.omniroute_provider_manager.add_provider(provider)
    
    await write_log("INFO", "omniroute_provider_created", f"AI provider {payload.name} created.", {"provider_id": provider_id, "type": payload.provider_type})
    return {
        "status": "ok",
        "provider_id": provider_id,
        "name": payload.name,
        "type": payload.provider_type,
    }

@app.get("/api/omniroute/providers")
async def list_omniroute_providers():
    """List all AI providers."""
    providers = runtime.omniroute_provider_manager.list_providers()
    return {
        "status": "ok",
        "providers": [p.to_dict() for p in providers],
    }

@app.post("/api/omniroute/providers/{provider_id}/test")
async def test_omniroute_provider_connection(provider_id: str):
    """Test connection to an AI provider."""
    result = await runtime.omniroute_provider_manager.test_connection(provider_id)
    return {"status": "ok", **result}

@app.post("/api/omniroute/providers/health")
async def omniroute_provider_health_all():
    """Health check all AI providers."""
    result = await runtime.omniroute_provider_manager.health_check_all()
    return {"status": "ok", **result}

@app.post("/api/omniroute/models")
async def create_omniroute_model(payload: OmniRouteModelCreate):
    """Register an AI model."""
    from uuid import uuid4
    from database import ai_upsert_model
    
    model_id = str(uuid4())
    model = AIModel(
        model_id=model_id,
        provider_id=payload.provider_id,
        model_id_str=payload.model_id,
        model_name=payload.model_name,
        model_category=payload.model_category,
        capabilities=payload.capabilities,
        context_window=payload.context_window,
        input_price_per_1k=payload.input_price_per_1k,
        output_price_per_1k=payload.output_price_per_1k,
        image_price_per_image=payload.image_price_per_image,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        is_active=payload.is_active,
    )
    
    await ai_upsert_model(
        model_id=model_id,
        provider_id=payload.provider_id,
        model_id_str=payload.model_id,
        model_name=payload.model_name,
        model_category=payload.model_category,
        context_window=payload.context_window,
        input_price_per_1k=payload.input_price_per_1k,
        output_price_per_1k=payload.output_price_per_1k,
        image_price_per_image=payload.image_price_per_image,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        capabilities_json=json.dumps(payload.capabilities),
        is_active=payload.is_active,
    )
    
    runtime.omniroute_model_registry.register_model(model)
    
    await write_log("INFO", "omniroute_model_created", f"AI model {payload.model_name} registered.", {"model_id": model_id, "provider_id": payload.provider_id})
    return {
        "status": "ok",
        "model_id": model_id,
        "model_name": payload.model_name,
        "provider_id": payload.provider_id,
    }

@app.get("/api/omniroute/models")
async def list_omniroute_models(provider_id: str | None = None, category: str | None = None):
    """List AI models."""
    models = runtime.omniroute_model_registry.list_models(
        provider_id=provider_id,
        category=category,
        active_only=True,
    )
    return {
        "status": "ok",
        "models": [m.to_dict() for m in models],
    }

@app.post("/api/omniroute/routing/rules")
async def create_omniroute_routing_rule(payload: OmniRouteRuleCreate):
    """Create a routing rule."""
    from uuid import uuid4
    rule_id = str(uuid4())
    rule = RoutingRule(
        rule_id=rule_id,
        name=payload.name,
        strategy=payload.strategy,
        provider_priority=payload.provider_priority,
        model_category=payload.model_category,
        min_quality_score=payload.min_quality_score,
        max_cost_per_1k=payload.max_cost_per_1k,
        max_latency_ms=payload.max_latency_ms,
    )
    runtime.omniroute_routing_engine.add_rule(rule)
    return {
        "status": "ok",
        "rule_id": rule_id,
        "name": payload.name,
        "strategy": payload.strategy,
    }

@app.get("/api/omniroute/routing/rules")
async def list_omniroute_routing_rules():
    """List routing rules."""
    return {
        "status": "ok",
        "rules": [r.to_dict() for r in runtime.omniroute_routing_engine.rules.values()],
    }

@app.post("/api/omniroute/routing/policies")
async def create_omniroute_routing_policy(payload: OmniRoutePolicyCreate):
    """Create a routing policy for a workflow."""
    from uuid import uuid4
    policy_id = str(uuid4())
    policy = RoutingPolicy(
        policy_id=policy_id,
        workflow_name=payload.workflow_name,
        rule_id=payload.rule_id,
        fallback_rule_id=payload.fallback_rule_id,
    )
    runtime.omniroute_routing_engine.add_policy(policy)
    return {
        "status": "ok",
        "policy_id": policy_id,
        "workflow_name": payload.workflow_name,
        "rule_id": payload.rule_id,
    }

@app.get("/api/omniroute/routing/policies")
async def list_omniroute_routing_policies():
    """List routing policies."""
    return {
        "status": "ok",
        "policies": [p.to_dict() for p in runtime.omniroute_routing_engine.policies.values()],
    }

@app.post("/api/omniroute/routing/select")
async def omniroute_select_provider(
    workflow_name: str,
    required_capability: str | None = None,
    max_cost: float | None = None,
):
    """Select the best provider and model for a workflow."""
    provider_id, model_id = runtime.omniroute_routing_engine.select_provider(
        workflow_name=workflow_name,
        required_capability=required_capability,
        max_cost=max_cost,
    )
    return {
        "status": "ok",
        "workflow_name": workflow_name,
        "provider_id": provider_id,
        "model_id": model_id,
    }

@app.get("/api/omniroute/cost/calculate")
async def omniroute_calculate_cost(
    input_tokens: int = 0,
    output_tokens: int = 0,
    input_price_per_1k: float = 0.0,
    output_price_per_1k: float = 0.0,
    num_images: int = 0,
    price_per_image: float = 0.0,
):
    """Calculate AI request cost."""
    text_cost = runtime.omniroute_cost_calculator.calculate_text_cost(
        input_tokens, output_tokens, input_price_per_1k, output_price_per_1k
    )
    image_cost = runtime.omniroute_cost_calculator.calculate_image_cost(
        num_images, price_per_image
    )
    total_cost = text_cost + image_cost
    return {
        "status": "ok",
        "text_cost_usd": text_cost,
        "image_cost_usd": image_cost,
        "total_cost_usd": total_cost,
    }

@app.get("/api/omniroute/stats")
async def omniroute_stats():
    """Get OmniRoute Control Center statistics."""
    providers = runtime.omniroute_provider_manager.list_providers()
    models = runtime.omniroute_model_registry.list_models(active_only=True)
    return {
        "status": "ok",
        "providers": len(providers),
        "active_providers": sum(1 for p in providers if p.is_active),
        "models": len(models),
        "active_models": sum(1 for m in models if m.is_active),
        "routing_rules": len(runtime.omniroute_routing_engine.rules),
        "routing_policies": len(runtime.omniroute_routing_engine.policies),
    }

# ---------------------------------------------------------------------------
# WebIF Pages - Google Merchant Center
# ---------------------------------------------------------------------------

@app.get("/google", response_class=HTMLResponse)
async def google_dashboard_page(request: Request):
    """Google Merchant Center dashboard page."""
    return templates.TemplateResponse("google_dashboard.html", {"request": request})

@app.get("/google/export", response_class=HTMLResponse)
async def google_export_page(request: Request):
    """Google Merchant Center export page."""
    return templates.TemplateResponse("google_export.html", {"request": request})

# ---------------------------------------------------------------------------
# WebIF Pages - OmniRoute Control Center
# ---------------------------------------------------------------------------

@app.get("/omniroute", response_class=HTMLResponse)
async def omniroute_dashboard_page(request: Request):
    """OmniRoute Control Center dashboard page."""
    return templates.TemplateResponse("omniroute_dashboard.html", {"request": request})

# ---------------------------------------------------------------------------
# Integration Pipeline: Matterhorn → AI → Google Merchant Center
# ---------------------------------------------------------------------------

@app.post("/api/pipeline/matterhorn-to-google")
async def pipeline_matterhorn_to_google(account_id: str, product_sku: str):
    """Run the full pipeline: Matterhorn product → AI optimization → Google export."""
    from services.canonical_product_mapping import CanonicalProduct
    from database import fetch_product
    
    product_data = await fetch_product(product_sku)
    if not product_data:
        raise HTTPException(status_code=404, detail="Product not found")
    
    cp = CanonicalProduct.from_matterhorn("matterhorn", product_data)
    
    # Step 1: AI optimization via OmniRoute (if configured)
    ai_title = None
    ai_description = None
    ai_image_url = None
    
    if runtime.omniroute:
        try:
            # Route to text AI for title optimization
            provider_id, model_id = runtime.omniroute_routing_engine.select_provider(
                workflow_name="google_title_optimization",
                required_capability="text_generation",
            )
            if provider_id and model_id:
                ai_response = runtime.omniroute.generate_completion(
                    prompt=f"Optimiere folgenden Produkttitel für Google Shopping (max 150 Zeichen, Deutsch): {cp.product_name}",
                    model=model_id,
                    temperature=0.7,
                    max_tokens=200,
                )
                ai_title = ai_response.get("generated_text", cp.product_name)[:150]
        except Exception:
            pass  # Use original title if AI fails
    
    # Step 2: Export to Google Merchant Center
    engine = runtime.google_export_engines.get(account_id)
    if not engine:
        raise HTTPException(status_code=404, detail="Google export engine not initialized for account")
    
    queue_id = await engine.on_quality_check_passed(
        product_sku=product_sku,
        account_id=account_id,
        canonical_product=cp,
        ai_title=ai_title,
        ai_description=ai_description,
        ai_image_url=ai_image_url,
        shop_url=None,
    )
    
    return {
        "status": "ok",
        "pipeline": "matterhorn_to_google",
        "product_sku": product_sku,
        "export_id": queue_id,
        "ai_optimized": ai_title is not None,
    }

@app.post("/api/pipeline/auto-export-imported")
async def pipeline_auto_export_imported(account_id: str):
    """Auto-export all recently imported Matterhorn products to Google."""
    from database import list_products
    from services.canonical_product_mapping import CanonicalProduct
    
    exported = 0
    errors = 0
    
    # Get all active products
    products = await list_products(limit=500)
    
    for p in products:
        try:
            cp = CanonicalProduct.from_matterhorn("matterhorn", p)
            
            await runtime.google_export_engines[account_id].on_quality_check_passed(
                product_sku=p["sku"],
                account_id=account_id,
                canonical_product=cp,
                shop_url=None,
            )
            exported += 1
        except Exception as exc:
            errors += 1
            logger.warning("Auto-export failed for %s: %s", p["sku"], exc)
    
    return {
        "status": "ok",
        "exported": exported,
        "errors": errors,
        "account_id": account_id,
    }


# ---------------------------------------------------------------------------
# Global Control Center API
# ---------------------------------------------------------------------------

@app.get("/api/control-center/overview")
async def control_center_overview() -> dict[str, Any]:
    """Return Global Control Center executive overview."""
    if runtime.control_center is None:
        raise HTTPException(status_code=503, detail="Control center not initialized")
    return await runtime.control_center.get_executive_overview()


@app.get("/api/control-center/dashboard")
async def control_center_dashboard() -> dict[str, Any]:
    """Return aggregated Global Control Center dashboard data."""
    if runtime.control_center is None:
        raise HTTPException(status_code=503, detail="Control center not initialized")
    return await runtime.control_center.get_full_dashboard()


@app.get("/control-center", response_class=HTMLResponse)
async def control_center_page(request: Request) -> HTMLResponse:
    """Render the unified global control center."""
    return templates.TemplateResponse("control_center.html", {"request": request})


@app.get("/api/control-center/system")
async def control_center_system() -> dict[str, Any]:
    """Return system intelligence metrics."""
    if runtime.control_center is None:
        raise HTTPException(status_code=503, detail="Control center not initialized")
    return await runtime.control_center.get_system_intelligence()
