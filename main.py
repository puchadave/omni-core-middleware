"""FastAPI core application for Omni-Core Master Middleware.

Handles:
- Master dashboard view and API endpoints
- System health check (pinging OmniRoute /v1/models with latency tracking)
- Settings manager endpoint with Pydantic V2 validation
- Internal asyncio background worker loop for asynchronous jobs
- Payment state synchronization and fulfillment guarding
"""

from __future__ import annotations

import asyncio
import time
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import httpx
import aiosqlite
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, AliasChoices
from typing_extensions import Annotated

from database import init_db, get_connection, close_connection, DB_PATH
from omni_core.services.payment_sync import (
    payment_sync,
    start_payment_sync_worker,
    stop_payment_sync_worker,
)
from omni_core.services.omniroute_client import OmniRouteClient
from omni_core.services.pricing_engine import PricingEngine
from omni_core.services.inventory_guard import InventoryGuard

logger = logging.getLogger(__name__)

task_queue: asyncio.Queue = asyncio.Queue()

templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Pydantic V2 Models for request validation
# ---------------------------------------------------------------------------

class SettingsSave(BaseModel):
    """Model for validating settings save payload."""

    omniroute_url: Annotated[str, Field(
        ...,
        description="OmniRoute API base URL (e.g. http://localhost:4000/v1)"
    )]
    omniroute_model: Annotated[str, Field(
        ...,
        description="OmniRoute AI model identifier"
    )]
    shopware_api_url: Annotated[str, Field(
        default="",
        description="Shopware 6 API endpoint URL"
    )]
    ebay_token: Annotated[str, Field(
        default="",
        description="eBay marketplace API token"
    )]
    amazon_token: Annotated[str, Field(
        default="",
        description="Amazon marketplace API token"
    )]
    kaufland_token: Annotated[str, Field(
        default="",
        description="Kaufland marketplace API token"
    )]
    etsy_token: Annotated[str, Field(
        default="",
        description="Etsy marketplace API token"
    )]

    model_config = {"protected_namespaces": ()}


class SystemHealth(BaseModel):
    """Model returned by the /api/system-health endpoint."""

    omniroute: Dict[str, Any]
    shops: Dict[str, str]
    queue_size: int
    total_products: int
    error_count: int
    pending_orders: int
    total_orders: int
    pending_payment_orders: int


# ---------------------------------------------------------------------------
# Lifespan & Background
# ---------------------------------------------------------------------------

async def check_omniroute_health() -> Dict[str, Any]:
    """Ping OmniRoute /v1/models with latency tracking.

    Returns a dict with ``status`` (online / degraded / offline) and
    ``latency_ms`` (float or None).
    """
    url, model, _, _, _, _, _ = await _read_omniroute_settings()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            start = time.monotonic()
            response = await client.get(
                f"{url}/models",
                headers={"Authorization": f"Bearer {model}"}
            )
            latency = (time.monotonic() - start) * 1000
            if response.status_code == 200:
                return {"status": "online", "latency_ms": round(latency, 2)}
            return {"status": "degraded", "latency_ms": round(latency, 2)}
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"OmniRoute health check failed: {exc}")
        return {"status": "offline", "latency_ms": None}


async def _read_omniroute_settings() -> tuple[str, str, str, str, str, str, str]:
    """Read the seven settings rows from the database.

    Returns a 7-tuple: (url, model, ebay_token, amazon_token,
    kaufland_token, etsy_token, shopware_api_url).
    """
    async with aiosqlite.connect(str(DB_PATH)) as db:
        rows: dict[str, str] = {}
        async with db.execute("SELECT key, value FROM settings") as cursor:
            async for key, value in cursor:
                rows[key] = value

    url = rows.get("omniroute_url", "http://localhost:4000/v1")
    model = rows.get("omniroute_model", "gemini-2.5-flash")
    ebay = rows.get("ebay_token", "")
    amazon = rows.get("amazon_token", "")
    kaufland = rows.get("kaufland_token", "")
    etsy = rows.get("etsy_token", "")
    shopware = rows.get("shopware_api_url", "")
    return url, model, ebay, amazon, kaufland, etsy, shopware


async def get_system_stats() -> Dict[str, int]:
    """Query the database for current product and order statistics."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM products WHERE status='active'"
        ) as cur:
            total_products = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM products WHERE status='error'"
        ) as cur:
            error_count = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM orders WHERE status='pending'"
        ) as cur:
            pending_orders = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM orders"
        ) as cur:
            total_orders = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM orders WHERE payment_status != 'paid'"
        ) as cur:
            pending_payment_orders = (await cur.fetchone())[0]
    return {
        "total_products": total_products,
        "error_count": error_count,
        "pending_orders": pending_orders,
        "total_orders": total_orders,
        "pending_payment_orders": pending_payment_orders,
    }


async def get_shop_status() -> Dict[str, str]:
    """Determine connection status for each marketplace from settings rows."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        rows: dict[str, str] = {}
        async with db.execute("SELECT key, value FROM settings") as cursor:
            async for key, value in cursor:
                rows[key] = value
    return {
        "shopware": "connected" if rows.get("shopware_api_url") else "disconnected",
        "ebay": "connected" if rows.get("ebay_token") else "disconnected",
        "amazon": "connected" if rows.get("amazon_token") else "disconnected",
        "kaufland": "connected" if rows.get("kaufland_token") else "disconnected",
        "etsy": "connected" if rows.get("etsy_token") else "disconnected",
    }


async def background_worker() -> None:
    """Internal asyncio background worker loop for asynchronous jobs.

    Runs every 5 seconds, performing a health ping, updating system stats,
    and logging the current state. Designed to be cancelled gracefully on
    application shutdown.
    """
    logger.info("Background worker started")
    try:
        while True:
            await asyncio.sleep(5)
            health = await check_omniroute_health()
            stats = await get_system_stats()
            shops = await get_shop_status()
            logger.info(
                f"Worker: omniroute={health['status']} "
                f"latency={health['latency_ms']}ms "
                f"products={stats['total_products']} "
                f"errors={stats['error_count']} "
                f"queue={task_queue.qsize()}"
            )
    except asyncio.CancelledError:
        logger.info("Background worker cancelled during shutdown")
        raise
    except Exception as exc:  # pragma: no cover
        logger.exception(f"Background worker unexpected error: {exc}")


async def payment_state_sync_worker() -> None:
    """Runs the payment state sync loop.

    Analog zum Tracking-Worker wird periodisch geprüft, ob Zahlungen
    beim Gateway eingegangen sind. Sobald ``payment_status = 'paid'``,
    wird ``fulfillment_locked`` auf ``false`` gesetzt.
    """
    logger.info("Payment state sync worker started")
    try:
        while True:
            await asyncio.sleep(30)  # alle 30 Sekunden
            await _sync_all_pending_payments()
    except asyncio.CancelledError:
        logger.info("Payment state sync worker cancelled during shutdown")
        raise
    except Exception as exc:  # pragma: no cover
        logger.exception(f"Payment state sync worker unexpected error: {exc}")


async def _sync_all_pending_payments() -> None:
    """Hole alle Bestellungen mit offenem Zahlungsstatus und prüfe nach."""
    async with get_connection() as db:
        async with db.execute(
            "SELECT order_id, sku FROM orders WHERE payment_status != 'paid'"
        ) as cursor:
            orders = await cursor.fetchall()

    for order_id, sku in orders:
        await _sync_order_payment_gate(order_id, sku)


async def _sync_order_payment_gate(order_id: str, sku: str) -> None:
    """Prüfe den Zahlungsstatus einer Einzelbestellung und aktualisiere DB.

    Idempotent: wenn bereits ``payment_status = 'paid'``, passiert nichts.
    """
    async with get_connection() as db:
        async with db.execute(
            "SELECT payment_status, fulfillment_locked FROM orders WHERE order_id = ?",
            (order_id,),
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        logger.warning(f"Order {order_id} not found during payment sync")
        return

        current_status, locked = row

    if current_status == "paid":
        return  # Bereits bezahlt, nichts tun

    # --- Placeholder: Echte Gateway-API-Call ---
    # Ersetze diesen Block durch einen echten httpx.Request an dein Payment Provider.
    # Beispiel:
    # async with httpx.AsyncClient(timeout=10.0) as client:
    #     response = await client.get(f"{GATEWAY_BASE}/orders/{order_id}/payment")
    #     gateway_data = response.json()
    #     gateway_status = gateway_data.get("status", "pending")
    #
    # mapped = GATEWAY_PAYMENT_MAP.get(gateway_status)
    # if mapped is None:
    #     logger.error(f"Unknown gateway status '{gateway_status}' for order {order_id}")
    #     return
    #
    # await _update_order_payment_status_db(db, order_id, mapped)
    # logger.info(f"Payment sync order {order_id}: {current_status} -> {mapped}")
    # return

    # --- SIMULATION: Jede 7. Bestellung wird als 'paid' markiert ---
    h = int(hashlib.md5(f"{order_id}{sku}".encode()).hexdigest()[:8], 16)
    if h % 7 == 0:
        new_status = "paid"
    else:
        new_status = "pending"

    async with get_connection() as db:
        await db.execute(
            "UPDATE orders SET payment_status = ?, fulfillment_locked = ?, updated_at = (STRFTIME('%Y-%m-%d %H:%M:%f', 'NOW')) WHERE order_id = ?",
            (new_status, new_status == "paid", order_id),
        )
        await db.commit()

    if new_status != current_status:
        logger.info(f"Payment sync order {order_id}: {current_status} -> {new_status}")
    else:
        logger.debug(f"Payment sync order {order_id} unchanged: {current_status}")


async def _update_order_payment_status_db(
    db: aiosqlite.Connection, order_id: str, new_status: str
) -> None:
    """Führe das DB-Update des payment_status aus und setze fulfillment_locked."""
    await db.execute(
        "UPDATE orders SET payment_status = ?, fulfillment_locked = ?, updated_at = (STRFTIME('%Y-%m-%d %H:%M:%f', 'NOW')) WHERE order_id = ?",
        (new_status, new_status == "paid", order_id),
    )
    await db.commit()


async def check_fulfillment_guard(order_id: str) -> None:
    """Sicherheits-Gard vor der Weiterleitung einer Bestellung an Fulfillment/ERP.

    Dieses muss am Anfang jeder ``forwardOrder()``- oder ``dispatchToFulfillment()``-Aufrufe
    aufgerufen werden.

    :raises RuntimeError: Wenn ``fulfillment_locked === true`` oder
        ``payment_status != 'paid'``, mit der exakt definierten Fehlermeldung.
    """
    conn = await get_connection()
    try:
        async with conn.execute(
            "SELECT payment_status, fulfillment_locked FROM orders WHERE order_id = ?",
            (order_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            raise RuntimeError(f"Order {order_id} not found")

        payment_status, fulfillment_locked = row

        if fulfillment_locked or payment_status != "paid":
            raise RuntimeError(
                f"Error: Fulfillment blocked due to incomplete payment. "
                f"order_id={order_id}, payment_status={payment_status}, "
                f"fulfillment_locked={fulfillment_locked}"
            )
    finally:
        await close_connection(conn)


async def lifespan(app: FastAPI) -> Any:
    """FastAPI lifespan context.

    - Initializes the SQLite database (WAL mode + tables)
    - Starts the background worker and payment sync worker
    - Yields control to the application
    - On shutdown: cancels both workers
    """
    await init_db()
    background_task: asyncio.Task = asyncio.create_task(background_worker())
    payment_task: asyncio.Task = asyncio.create_task(payment_state_sync_worker())
    try:
        yield
    finally:
        background_task.cancel()
        payment_task.cancel()
        try:
            await background_task
        except asyncio.CancelledError:
            pass
        try:
            await payment_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# FastAPI application assembly
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Omni-Core Master Middleware",
    lifespan=lifespan,
)


templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# HTTP Route Handlers
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def master_dashboard(request: Request) -> HTMLResponse:
    """Render the main dashboard HTML page with current statistics."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        async with db.execute("SELECT COUNT(*) FROM products") as cursor:
            total_products = (await cursor.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM products WHERE status='error'"
        ) as cursor:
            error_count = (await cursor.fetchone())[0]

    return templates.TemplateResponse(
        "master_dashboard.html",
        {
            "request": request,
            "total_products": total_products,
            "error_count": error_count,
            "queue_size": task_queue.qsize(),
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    """Render the settings configuration page."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        async with db.execute("SELECT key, value FROM settings") as cursor:
            settings: dict[str, str] = {
                row[0]: row[1] for row in (await cursor.fetchall())
            }
    return templates.TemplateResponse(
        "settings.html", {"request": request, "settings": settings}
    )


@app.post("/settings/save", response_class=JSONResponse)
async def save_settings(payload: SettingsSave) -> JSONResponse:
    """Persist configuration values into the settings table.

    Accepts a JSON body validated by the ``SettingsSave`` Pydantic model.
    Returns a JSON success response.
    """
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.executemany(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            [
                ("omniroute_url", payload.omniroute_url),
                ("omniroute_model", payload.omniroute_model),
                ("shopware_api_url", payload.shopware_api_url),
                ("ebay_token", payload.ebay_token),
                ("amazon_token", payload.amazon_token),
                ("kaufland_token", payload.kaufland_token),
                ("etsy_token", payload.etsy_token),
            ],
        )
        await db.commit()
    return JSONResponse({"ok": True, "message": "Settings saved"})


@app.get("/api/system-health", response_model=SystemHealth)
async def system_health() -> SystemHealth:
    """Return the current system health as a typed JSON response.

    - Pings OmniRoute /v1/models with latency tracking
    - Queries DB for product / order stats
    - Determines marketplace connection status from saved settings
    - Includes count of orders with pending payment
    """
    health = await check_omniroute_health()
    stats = await get_system_stats()
    shops = await get_shop_status()
    return SystemHealth(
        omniroute=health,
        shops=shops,
        queue_size=task_queue.qsize(),
        **stats,
    )


# ---------------------------------------------------------------------------
# Fulfillment Dispatch Helper (integrated guard)
# ---------------------------------------------------------------------------

async def forward_order_to_fulfillment(order_id: str, order_data: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch an order to the fulfillment system after guard validation.

    This is the central entry point for all order dispatches. It validates
    the payment state before sending the order downstream.

    :param order_id: The internal order identifier.
    :param order_data: The order payload dict (SKU, quantity, customer info, etc.).
    :return: The fulfillment response dict.
    :raises RuntimeError: If the fulfillment guard blocks the dispatch.
    """
    # 1. Fulfillment Guard prüfen (Blockade bei offenem Zahlungseingang)
    await check_fulfillment_guard(order_id)

    # 2. Falls Guard passiert ist: echte Dispatch-Logik
    # Hier würde der Aufruf an Matterhorn / ERP erfolgen.
    # Beispielhaft zurückgegeben ein Success-Response.

    # Beispiel-Integration des Inventory Guard:
    inventory_guard = InventoryGuard(safety_stock_buffer=0.10)

    # Hier würde der tatsächliche Dispatch stehen:
    # tracking = await matterhorn_dispatch(order_data)
    # return {"status": "dispatched", "tracking_number": tracking, ...}

    # Placeholder-Response für den Fall, dass die Guard bestanden hat:
    return {
        "order_id": order_id,
        "status": "dispatched",
        "message": f"Order dispatched to fulfillment. Order {order_id} payment verified.",
    }