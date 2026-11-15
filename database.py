"""SQLite persistence layer for Omni-Core Middleware.

WAL-mode aiosqlite with short-lived connections, atomic transactions
and bounded queries for minimal RAM footprint on Alpine LXC.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

import aiosqlite

logger = logging.getLogger(__name__)

DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "middleware.db"))
BUSY_TIMEOUT_MS = 5_000


class DatabaseError(RuntimeError):
    """Raised when a persistent state transition fails."""


class InsufficientStockError(DatabaseError):
    """Raised when an order cannot be reserved without overselling."""


def utc_now() -> str:
    """ISO-8601 UTC timestamp for TEXT columns."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _connect() -> aiosqlite.Connection:
    """Open one configured SQLite connection."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(DATABASE_PATH), timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    await conn.execute("PRAGMA foreign_keys=ON")
    return conn


@asynccontextmanager
async def get_connection() -> AsyncIterator[aiosqlite.Connection]:
    """Yield a configured connection; always closed on exit."""
    conn = await _connect()
    try:
        yield conn
    finally:
        await conn.close()


async def close_connection(conn: aiosqlite.Connection | None) -> None:
    """Safely close a connection (None-safe for backwards compatibility)."""
    if conn is not None:
        await conn.close()


async def init_db() -> None:
    """Create schema and enable WAL mode. Idempotent."""
    async with get_connection() as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                is_secret INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0,1)),
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS products (
                sku TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                raw_description TEXT NOT NULL DEFAULT '',
                ai_title TEXT NOT NULL DEFAULT '',
                ai_description_html TEXT NOT NULL DEFAULT '',
                ai_tags_json TEXT NOT NULL DEFAULT '[]',
                supplier_price REAL NOT NULL DEFAULT 0.0 CHECK (supplier_price >= 0),
                stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
                is_bundle INTEGER NOT NULL DEFAULT 0 CHECK (is_bundle IN (0,1)),
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('draft','active','paused','archived')),
                source_hash TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_products_status_stock
                ON products(status, stock, is_bundle);

            CREATE TABLE IF NOT EXISTS bundles (
                bundle_sku TEXT PRIMARY KEY,
                component_skus_json TEXT NOT NULL,
                title TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (bundle_sku) REFERENCES products(sku) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                sku TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                marketplace TEXT NOT NULL,
                customer_reference TEXT NOT NULL DEFAULT '',
                payment_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (payment_status IN ('pending','paid','failed','refunded')),
                fulfillment_locked INTEGER NOT NULL DEFAULT 0
                    CHECK (fulfillment_locked IN (0,1)),
                status TEXT NOT NULL DEFAULT 'received'
                    CHECK (status IN ('received','reserved','dispatching','dispatched','failed','cancelled')),
                tracking_number TEXT NOT NULL DEFAULT '',
                tracking_url TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (sku) REFERENCES products(sku)
            );
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, updated_at);

            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL CHECK (level IN ('DEBUG','INFO','WARNING','ERROR')),
                event TEXT NOT NULL,
                message TEXT NOT NULL,
                context_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_logs_created_at ON logs(created_at DESC);

            CREATE TABLE IF NOT EXISTS import_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                imported INTEGER NOT NULL DEFAULT 0,
                updated INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','success','failed')),
                error_message TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS channel_overrides (
                sku TEXT,
                channel_name TEXT,             -- z.B. 'shopify', 'amazon', 'google_shopping'
                custom_title TEXT,
                custom_description TEXT,
                custom_price REAL,
                is_published BOOLEAN DEFAULT 1,
                PRIMARY KEY (sku, channel_name),
                FOREIGN KEY (sku) REFERENCES products(sku) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS system_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT,
                event_type TEXT,               -- z.B. 'price_adjustment', 'stock_warning', 'ai_generated'
                event_data TEXT,               -- JSON mit Details
                created_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP)
            );
            """
        )
        await db.commit()
    logger.info("Database initialized (WAL mode, path=%s)", DATABASE_PATH)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

async def set_setting(key: str, value: str, *, is_secret: bool = False) -> None:
    """Idempotent upsert of a setting."""
    if not key or not key.replace("_", "").isalnum():
        raise ValueError("Setting keys may only contain letters, digits, underscores.")
    async with get_connection() as db:
        await db.execute(
            """
            INSERT INTO settings(key, value, is_secret, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                is_secret = excluded.is_secret,
                updated_at = excluded.updated_at
            """,
            (key, value, int(is_secret), utc_now()),
        )
        await db.commit()


async def get_setting(key: str) -> str | None:
    """Get raw setting value (server-side only, never rendered to UI)."""
    async with get_connection() as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
    return str(row["value"]) if row else None


async def list_settings(*, reveal_secrets: bool = False) -> dict[str, str]:
    """List settings; secrets masked unless explicitly revealed."""
    async with get_connection() as db:
        cursor = await db.execute("SELECT key, value, is_secret FROM settings ORDER BY key")
        rows = await cursor.fetchall()
        await cursor.close()
    return {
        str(row["key"]): str(row["value"]) if reveal_secrets or not row["is_secret"] else "********"
        for row in rows
    }


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

async def write_log(level: str, event: str, message: str, context: Mapping[str, Any] | None = None) -> None:
    """Store a bounded structured log record without secrets."""
    payload = json.dumps(context or {}, ensure_ascii=False, separators=(",", ":"))[:4_096]
    async with get_connection() as db:
        await db.execute(
            "INSERT INTO logs(level, event, message, context_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (level.upper(), event, message[:2_000], payload, utc_now()),
        )
        await db.commit()


async def recent_logs(limit: int = 50) -> list[dict[str, Any]]:
    """Newest operational records for the dashboard."""
    safe_limit = min(max(limit, 1), 200)
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT id, level, event, message, context_json, created_at FROM logs ORDER BY id DESC LIMIT ?",
            (safe_limit,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Products & Bundles
# ---------------------------------------------------------------------------

async def upsert_product(
    *,
    sku: str,
    title: str,
    category: str,
    supplier_price: float,
    stock: int,
    source_hash: str,
    raw_description: str = "",
) -> bool:
    """Idempotent supplier product upsert. Returns True if state changed."""
    if supplier_price < 0 or stock < 0:
        raise ValueError("Price and stock must not be negative.")
    now = utc_now()
    async with get_connection() as db:
        cursor = await db.execute("SELECT source_hash FROM products WHERE sku = ?", (sku,))
        current = await cursor.fetchone()
        await cursor.close()
        if current and current["source_hash"] == source_hash:
            return False
        await db.execute(
            """
            INSERT INTO products(
                sku, title, category, raw_description, supplier_price, stock,
                is_bundle, status, source_hash, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 'active', ?, ?)
            ON CONFLICT(sku) DO UPDATE SET
                title = excluded.title,
                category = excluded.category,
                raw_description = excluded.raw_description,
                supplier_price = excluded.supplier_price,
                stock = excluded.stock,
                status = CASE WHEN products.is_bundle = 1 THEN products.status ELSE 'active' END,
                source_hash = excluded.source_hash,
                updated_at = excluded.updated_at
            """,
            (sku, title, category, raw_description, supplier_price, stock, source_hash, now),
        )
        await db.commit()
    return True


async def refresh_bundle_stock(db: aiosqlite.Connection, bundle_sku: str) -> tuple[int, str]:
    """Set bundle stock to min(component stocks); pause zero-stock bundles.

    Caller must hold an active write transaction.
    """
    cursor = await db.execute("SELECT component_skus_json FROM bundles WHERE bundle_sku = ?", (bundle_sku,))
    bundle = await cursor.fetchone()
    await cursor.close()
    if not bundle:
        raise DatabaseError(f"Bundle {bundle_sku!r} does not exist.")
    component_skus = json.loads(str(bundle["component_skus_json"]))
    if not isinstance(component_skus, list) or not component_skus:
        stock, status = 0, "paused"
    else:
        placeholders = ",".join("?" for _ in component_skus)
        cursor = await db.execute(
            f"SELECT sku, stock FROM products WHERE sku IN ({placeholders})", tuple(component_skus)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        stocks = {str(row["sku"]): int(row["stock"]) for row in rows}
        stock = min((stocks.get(str(sku), 0) for sku in component_skus), default=0)
        status = "active" if stock > 0 else "paused"
    await db.execute(
        "UPDATE products SET stock = ?, status = ?, updated_at = ? WHERE sku = ?",
        (stock, status, utc_now(), bundle_sku),
    )
    return stock, status


async def refresh_all_bundle_stocks() -> None:
    """Recalculate every bundle after ingest or reservation."""
    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("SELECT bundle_sku FROM bundles")
            bundle_skus = [str(row["bundle_sku"]) for row in await cursor.fetchall()]
            await cursor.close()
            for bundle_sku in bundle_skus:
                await refresh_bundle_stock(db, bundle_sku)
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def reserve_order_stock(order_id: str, sku: str, quantity: int) -> None:
    """Atomically reserve product or bundle components for one idempotent order."""
    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,))
            existing = await cursor.fetchone()
            await cursor.close()
            if existing and str(existing["status"]) in {"reserved", "dispatching", "dispatched"}:
                await db.commit()
                return

            cursor = await db.execute(
                "SELECT sku, stock, is_bundle, status FROM products WHERE sku = ?", (sku,)
            )
            product = await cursor.fetchone()
            await cursor.close()
            if not product or product["status"] != "active" or int(product["stock"]) < quantity:
                raise InsufficientStockError(f"Insufficient active stock for {sku}.")

            if int(product["is_bundle"]):
                cursor = await db.execute("SELECT component_skus_json FROM bundles WHERE bundle_sku = ?", (sku,))
                bundle = await cursor.fetchone()
                await cursor.close()
                if not bundle:
                    raise DatabaseError(f"Bundle metadata missing for {sku}.")
                component_skus = [str(item) for item in json.loads(str(bundle["component_skus_json"]))]
                for component_sku in component_skus:
                    cursor = await db.execute(
                        "UPDATE products SET stock = stock - ?, updated_at = ? "
                        "WHERE sku = ? AND is_bundle = 0 AND status = 'active' AND stock >= ?",
                        (quantity, utc_now(), component_sku, quantity),
                    )
                    changed = cursor.rowcount
                    await cursor.close()
                    if changed != 1:
                        raise InsufficientStockError(f"Insufficient component stock for {component_sku}.")
            else:
                cursor = await db.execute(
                    "UPDATE products SET stock = stock - ?, updated_at = ? "
                    "WHERE sku = ? AND status = 'active' AND stock >= ?",
                    (quantity, utc_now(), sku, quantity),
                )
                changed = cursor.rowcount
                await cursor.close()
                if changed != 1:
                    raise InsufficientStockError(f"Insufficient stock for {sku}.")

            cursor = await db.execute("SELECT bundle_sku FROM bundles")
            bundle_skus = [str(row["bundle_sku"]) for row in await cursor.fetchall()]
            await cursor.close()
            for bundle_sku in bundle_skus:
                await refresh_bundle_stock(db, bundle_sku)
            await db.execute(
                "UPDATE orders SET status = 'reserved', updated_at = ?, last_error = '' WHERE order_id = ?",
                (utc_now(), order_id),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def fetch_product(sku: str) -> dict[str, Any] | None:
    """Fetch one product."""
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM products WHERE sku = ?", (sku,))
        row = await cursor.fetchone()
        await cursor.close()
    return dict(row) if row else None


async def list_products(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Bounded product listing."""
    safe_limit, safe_offset = min(max(limit, 1), 500), max(offset, 0)
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT * FROM products ORDER BY updated_at DESC, sku ASC LIMIT ? OFFSET ?",
            (safe_limit, safe_offset),
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [dict(row) for row in rows]


async def count_dashboard_metrics() -> dict[str, int]:
    """Compact dashboard counters in one connection."""
    async with get_connection() as db:
        cursor = await db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM products) AS product_count,
                (SELECT COUNT(*) FROM products WHERE status = 'active') AS active_product_count,
                (SELECT COUNT(*) FROM orders WHERE status IN ('received','reserved','dispatching')) AS pending_order_count,
                (SELECT COUNT(*) FROM logs WHERE level = 'ERROR' AND created_at >= datetime('now', '-24 hours')) AS error_count
            """
        )
        row = await cursor.fetchone()
        await cursor.close()
    return {key: int(row[key]) for key in row.keys()} if row else {}


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

async def create_order(
    *,
    order_id: str,
    sku: str,
    quantity: int,
    marketplace: str,
    customer_reference: str = "",
) -> None:
    """Insert an order in 'received' state. Idempotent on order_id."""
    now = utc_now()
    async with get_connection() as db:
        await db.execute(
            """
            INSERT INTO orders(
                order_id, sku, quantity, marketplace, customer_reference,
                payment_status, fulfillment_locked, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, 'received', ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                quantity = excluded.quantity,
                marketplace = excluded.marketplace,
                customer_reference = excluded.customer_reference,
                updated_at = excluded.updated_at
            """,
            (order_id, sku, quantity, marketplace, customer_reference, now, now),
        )
        await db.commit()


async def update_order_status(order_id: str, status: str, *, tracking_number: str = "", tracking_url: str = "", error: str = "") -> None:
    """Update order status with optional tracking or error info."""
    async with get_connection() as db:
        await db.execute(
            """
            UPDATE orders SET status = ?, tracking_number = ?, tracking_url = ?,
                last_error = ?, updated_at = ? WHERE order_id = ?
            """,
            (status, tracking_number, tracking_url, error, utc_now(), order_id),
        )
        await db.commit()


async def list_orders(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Bounded order listing."""
    safe_limit, safe_offset = min(max(limit, 1), 500), max(offset, 0)
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT * FROM orders ORDER BY created_at DESC, order_id ASC LIMIT ? OFFSET ?",
            (safe_limit, safe_offset),
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Import runs
# ---------------------------------------------------------------------------

async def start_import_run(source: str) -> int:
    """Register a new import run; returns its row id."""
    async with get_connection() as db:
        cursor = await db.execute(
            "INSERT INTO import_runs(source, started_at, status) VALUES (?, ?, 'running')",
            (source, utc_now()),
        )
        await db.commit()
        run_id = int(cursor.lastrowid or 0)
        await cursor.close()
    return run_id


async def finish_import_run(run_id: int, *, imported: int, updated: int, errors: int, error_message: str = "") -> None:
    """Mark an import run as success or failed."""
    status = "success" if errors == 0 else "failed"
    async with get_connection() as db:
        await db.execute(
            """
            UPDATE import_runs SET finished_at = ?, imported = ?, updated = ?, errors = ?,
                status = ?, error_message = ? WHERE id = ?
            """,
            (utc_now(), imported, updated, errors, status, error_message, run_id),
        )
        await db.commit()


async def recent_import_runs(limit: int = 10) -> list[dict[str, Any]]:
    """Newest import runs for the dashboard."""
    safe_limit = min(max(limit, 1), 50)
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT * FROM import_runs ORDER BY id DESC LIMIT ?", (safe_limit,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [dict(row) for row in rows]
