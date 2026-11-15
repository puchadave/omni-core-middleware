"""Unified persistence layer for Omni-Core ERP & BI Middleware.

Production deployments use PostgreSQL through asyncpg. A SQLite WAL fallback is
kept for local development and edge installations. Public functions use the same
contract on either backend; write operations are short and transactional.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence

import aiosqlite

try:
    import asyncpg
except ImportError:  # Allows SQLite-only local development before dependencies install.
    asyncpg = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "middleware.db"))
BUSY_TIMEOUT_MS = 5_000
POSTGRES_RETRY_ATTEMPTS = 30
POSTGRES_RETRY_DELAY_SECONDS = 2
_pg_pool: Any | None = None


class DatabaseError(RuntimeError):
    """Raised when a persistent state transition cannot be completed."""


class InsufficientStockError(DatabaseError):
    """Raised when an order cannot reserve inventory without overselling."""


def uses_postgres() -> bool:
    """Return whether the configured backend is PostgreSQL."""
    return DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp suitable for both supported databases."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


POSTGRES_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT '',
        is_secret BOOLEAN NOT NULL DEFAULT FALSE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS products (
        sku TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '',
        raw_description TEXT NOT NULL DEFAULT '',
        ai_title TEXT NOT NULL DEFAULT '',
        ai_description_html TEXT NOT NULL DEFAULT '',
        ai_tags_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        supplier_price NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (supplier_price >= 0),
        stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
        is_bundle BOOLEAN NOT NULL DEFAULT FALSE,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('draft', 'active', 'paused', 'archived')),
        source_hash TEXT NOT NULL DEFAULT '',
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_products_status_stock ON products(status, stock, is_bundle)",
    "CREATE INDEX IF NOT EXISTS idx_products_ai_tags_json ON products USING GIN (ai_tags_json)",
    """
    CREATE TABLE IF NOT EXISTS bundles (
        bundle_sku TEXT PRIMARY KEY REFERENCES products(sku) ON DELETE CASCADE,
        component_skus_json JSONB NOT NULL,
        title TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bundles_components_json ON bundles USING GIN (component_skus_json)",
    """
    CREATE TABLE IF NOT EXISTS channel_content (
        id BIGSERIAL PRIMARY KEY,
        product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
        channel TEXT NOT NULL CHECK (channel IN ('shopware', 'ebay', 'amazon', 'kaufland', 'etsy')),
        content_type TEXT NOT NULL CHECK (content_type IN ('title', 'description', 'tags', 'image')),
        content_text TEXT NOT NULL DEFAULT '',
        metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        ai_generated BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(product_sku, channel, content_type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_channel_content_product_channel ON channel_content(product_sku, channel)",
    "CREATE INDEX IF NOT EXISTS idx_channel_content_metadata_json ON channel_content USING GIN (metadata_json)",
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        sku TEXT NOT NULL REFERENCES products(sku),
        quantity INTEGER NOT NULL CHECK (quantity > 0),
        marketplace TEXT NOT NULL,
        customer_reference TEXT NOT NULL DEFAULT '',
        payment_status TEXT NOT NULL DEFAULT 'pending' CHECK (payment_status IN ('pending', 'paid', 'failed', 'refunded')),
        fulfillment_locked BOOLEAN NOT NULL DEFAULT FALSE,
        status TEXT NOT NULL DEFAULT 'received' CHECK (status IN ('received', 'reserved', 'dispatching', 'dispatched', 'failed', 'cancelled')),
        tracking_number TEXT NOT NULL DEFAULT '',
        tracking_url TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_orders_status_updated ON orders(status, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS bi_metrics (
        id BIGSERIAL PRIMARY KEY,
        metric_name TEXT NOT NULL,
        metric_value NUMERIC(16,4) NOT NULL,
        metric_label TEXT NOT NULL DEFAULT '',
        dimensions_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bi_metrics_name_time ON bi_metrics(metric_name, recorded_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_bi_metrics_dimensions_json ON bi_metrics USING GIN (dimensions_json)",
    """
    CREATE TABLE IF NOT EXISTS system_logs (
        id BIGSERIAL PRIMARY KEY,
        level TEXT NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR')),
        event TEXT NOT NULL,
        message TEXT NOT NULL,
        context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_system_logs_created_at ON system_logs(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_system_logs_context_json ON system_logs USING GIN (context_json)",
    """
    CREATE TABLE IF NOT EXISTS import_runs (
        id BIGSERIAL PRIMARY KEY,
        source TEXT NOT NULL,
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at TIMESTAMPTZ,
        imported INTEGER NOT NULL DEFAULT 0,
        updated INTEGER NOT NULL DEFAULT 0,
        errors INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'success', 'failed')),
        error_message TEXT NOT NULL DEFAULT ''
    )
    """,
)

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    is_secret INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0, 1)),
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
    supplier_price REAL NOT NULL DEFAULT 0 CHECK (supplier_price >= 0),
    stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
    is_bundle INTEGER NOT NULL DEFAULT 0 CHECK (is_bundle IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('draft', 'active', 'paused', 'archived')),
    source_hash TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_status_stock ON products(status, stock, is_bundle);
CREATE TABLE IF NOT EXISTS bundles (
    bundle_sku TEXT PRIMARY KEY REFERENCES products(sku) ON DELETE CASCADE,
    component_skus_json TEXT NOT NULL,
    title TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    content_type TEXT NOT NULL,
    content_text TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    ai_generated INTEGER NOT NULL DEFAULT 0 CHECK (ai_generated IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(product_sku, channel, content_type)
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL REFERENCES products(sku),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    marketplace TEXT NOT NULL,
    customer_reference TEXT NOT NULL DEFAULT '',
    payment_status TEXT NOT NULL DEFAULT 'pending' CHECK (payment_status IN ('pending', 'paid', 'failed', 'refunded')),
    fulfillment_locked INTEGER NOT NULL DEFAULT 0 CHECK (fulfillment_locked IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'received' CHECK (status IN ('received', 'reserved', 'dispatching', 'dispatched', 'failed', 'cancelled')),
    tracking_number TEXT NOT NULL DEFAULT '',
    tracking_url TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status_updated ON orders(status, updated_at DESC);
CREATE TABLE IF NOT EXISTS bi_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    metric_label TEXT NOT NULL DEFAULT '',
    dimensions_json TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR')),
    event TEXT NOT NULL,
    message TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_system_logs_created_at ON system_logs(created_at DESC);
CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    imported INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'success', 'failed')),
    error_message TEXT NOT NULL DEFAULT ''
);
"""


async def get_pg_pool() -> Any:
    """Create a compact asyncpg pool, retrying while PostgreSQL starts."""
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    if asyncpg is None:
        raise DatabaseError("asyncpg is unavailable; install project requirements first.")
    if not uses_postgres():
        raise DatabaseError("DATABASE_URL must be postgresql://... for PostgreSQL mode.")

    for attempt in range(1, POSTGRES_RETRY_ATTEMPTS + 1):
        try:
            _pg_pool = await asyncpg.create_pool(
                dsn=DATABASE_URL,
                min_size=1,
                max_size=5,
                command_timeout=30,
                server_settings={"application_name": "omni-core"},
            )
            logger.info("PostgreSQL connection pool established on attempt %d.", attempt)
            return _pg_pool
        except Exception as exc:
            if attempt == POSTGRES_RETRY_ATTEMPTS:
                raise DatabaseError("PostgreSQL was not ready after %d attempts." % attempt) from exc
            logger.warning("PostgreSQL not ready (%d/%d): %s", attempt, POSTGRES_RETRY_ATTEMPTS, exc)
            await asyncio.sleep(POSTGRES_RETRY_DELAY_SECONDS)
    raise AssertionError("unreachable")


async def close_pg_pool() -> None:
    """Close the PostgreSQL pool if it has been created."""
    global _pg_pool
    if _pg_pool is not None:
        await _pg_pool.close()
        _pg_pool = None


async def _connect_sqlite() -> aiosqlite.Connection:
    """Open one configured SQLite WAL connection."""
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
    """Yield a SQLite connection for compatibility-only SQLite-specific callers."""
    if uses_postgres():
        raise DatabaseError("SQLite connection requested while PostgreSQL is configured.")
    conn = await _connect_sqlite()
    try:
        yield conn
    finally:
        await conn.close()


async def close_connection(conn: aiosqlite.Connection | None) -> None:
    """Close a SQLite connection; retained for backwards compatibility."""
    if conn is not None:
        await conn.close()


async def init_db() -> None:
    """Run automatic schema migrations for the selected storage backend."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                for statement in POSTGRES_SCHEMA:
                    await conn.execute(statement)
        logger.info("PostgreSQL schema migration complete.")
        return

    async with get_connection() as db:
        await db.executescript(SQLITE_SCHEMA)
        await db.commit()
    logger.info("SQLite WAL schema migration complete at %s.", DATABASE_PATH)


async def set_setting(key: str, value: str, *, is_secret: bool = False) -> None:
    """Idempotently store one setting."""
    if not key or not key.replace("_", "").isalnum():
        raise ValueError("Setting keys may only contain letters, digits, and underscores.")
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO settings(key, value, is_secret, updated_at) VALUES ($1, $2, $3, NOW())
                   ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value, is_secret = EXCLUDED.is_secret, updated_at = NOW()""",
                key, value, is_secret,
            )
        return
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO settings(key, value, is_secret, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, is_secret = excluded.is_secret, updated_at = excluded.updated_at""",
            (key, value, int(is_secret), utc_now()),
        )
        await db.commit()


async def get_setting(key: str) -> str | None:
    """Get a raw setting for trusted server-side code only."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM settings WHERE key = $1", key)
            return str(row["value"]) if row else None
    async with get_connection() as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
        return str(row["value"]) if row else None


async def list_settings(*, reveal_secrets: bool = False) -> dict[str, str]:
    """List settings while masking secret values by default."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value, is_secret FROM settings ORDER BY key")
        return {str(r["key"]): str(r["value"]) if reveal_secrets or not r["is_secret"] else "********" for r in rows}
    async with get_connection() as db:
        cursor = await db.execute("SELECT key, value, is_secret FROM settings ORDER BY key")
        rows = await cursor.fetchall()
        await cursor.close()
        return {str(r["key"]): str(r["value"]) if reveal_secrets or not r["is_secret"] else "********" for r in rows}


async def write_log(level: str, event: str, message: str, context: Mapping[str, Any] | None = None) -> None:
    """Persist one bounded structured log event."""
    safe_level = level.upper()
    if safe_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        safe_level = "ERROR"
    payload = json.dumps(context or {}, ensure_ascii=False, separators=(",", ":"))[:4096]
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO system_logs(level, event, message, context_json) VALUES ($1, $2, $3, $4::jsonb)",
                safe_level, event[:128], message[:2000], payload,
            )
        return
    async with get_connection() as db:
        await db.execute(
            "INSERT INTO system_logs(level, event, message, context_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (safe_level, event[:128], message[:2000], payload, utc_now()),
        )
        await db.commit()


async def recent_logs(limit: int = 50) -> list[dict[str, Any]]:
    """Return recent bounded log records."""
    safe_limit = min(max(limit, 1), 200)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, level, event, message, context_json, created_at FROM system_logs ORDER BY id DESC LIMIT $1", safe_limit)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT id, level, event, message, context_json, created_at FROM system_logs ORDER BY id DESC LIMIT ?", (safe_limit,))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def upsert_product(*, sku: str, title: str, category: str, supplier_price: float, stock: int, source_hash: str, raw_description: str = "") -> bool:
    """Idempotently persist a supplier product and report if its source changed."""
    if not sku or supplier_price < 0 or stock < 0:
        raise ValueError("SKU must be non-empty; price and stock cannot be negative.")
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow("SELECT source_hash FROM products WHERE sku = $1", sku)
            if existing and existing["source_hash"] == source_hash:
                return False
            await conn.execute(
                """INSERT INTO products(sku, title, category, raw_description, supplier_price, stock, source_hash, updated_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                   ON CONFLICT(sku) DO UPDATE SET
                     title=EXCLUDED.title, category=EXCLUDED.category, raw_description=EXCLUDED.raw_description,
                     supplier_price=EXCLUDED.supplier_price, stock=EXCLUDED.stock, source_hash=EXCLUDED.source_hash,
                     status=CASE WHEN products.is_bundle THEN products.status ELSE 'active' END, updated_at=NOW()""",
                sku, title, category, raw_description, supplier_price, stock, source_hash,
            )
        return True
    async with get_connection() as db:
        cursor = await db.execute("SELECT source_hash FROM products WHERE sku = ?", (sku,))
        existing = await cursor.fetchone()
        await cursor.close()
        if existing and existing["source_hash"] == source_hash:
            return False
        await db.execute(
            """INSERT INTO products(sku, title, category, raw_description, supplier_price, stock, source_hash, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sku) DO UPDATE SET
                 title=excluded.title, category=excluded.category, raw_description=excluded.raw_description,
                 supplier_price=excluded.supplier_price, stock=excluded.stock, source_hash=excluded.source_hash,
                 status=CASE WHEN products.is_bundle = 1 THEN products.status ELSE 'active' END, updated_at=excluded.updated_at""",
            (sku, title, category, raw_description, supplier_price, stock, source_hash, utc_now()),
        )
        await db.commit()
        return True


async def list_products(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Return products with bounded pagination."""
    safe_limit, safe_offset = min(max(limit, 1), 500), max(offset, 0)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM products ORDER BY updated_at DESC, sku ASC LIMIT $1 OFFSET $2", safe_limit, safe_offset)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM products ORDER BY updated_at DESC, sku ASC LIMIT ? OFFSET ?", (safe_limit, safe_offset))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def fetch_product(sku: str) -> dict[str, Any] | None:
    """Return one product by SKU."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM products WHERE sku = $1", sku)
        return dict(row) if row else None
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM products WHERE sku = ?", (sku,))
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None


async def list_bundles() -> list[dict[str, Any]]:
    """Return all bundle definitions."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM bundles ORDER BY bundle_sku")
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM bundles ORDER BY bundle_sku")
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def upsert_bundle(bundle_sku: str, title: str, component_skus: Sequence[str]) -> None:
    """Create/update a bundle definition; its stock is recalculated atomically."""
    components = [str(item) for item in component_skus if str(item)]
    if not components:
        raise ValueError("A bundle must contain at least one component.")
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO products(sku, title, category, supplier_price, stock, is_bundle, status, source_hash, updated_at)
                       VALUES ($1, $2, 'Bundle', 0, 0, TRUE, 'paused', $3, NOW())
                       ON CONFLICT(sku) DO UPDATE SET title=EXCLUDED.title, is_bundle=TRUE, updated_at=NOW()""",
                    bundle_sku, title, json.dumps(components, separators=(",", ":")),
                )
                await conn.execute(
                    """INSERT INTO bundles(bundle_sku, component_skus_json, title, updated_at) VALUES ($1, $2::jsonb, $3, NOW())
                       ON CONFLICT(bundle_sku) DO UPDATE SET component_skus_json=EXCLUDED.component_skus_json, title=EXCLUDED.title, updated_at=NOW()""",
                    bundle_sku, json.dumps(components), title,
                )
                await _refresh_bundle_stock_pg(conn, bundle_sku)
        return
    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                """INSERT INTO products(sku, title, category, supplier_price, stock, is_bundle, status, source_hash, updated_at)
                   VALUES (?, ?, 'Bundle', 0, 0, 1, 'paused', ?, ?)
                   ON CONFLICT(sku) DO UPDATE SET title=excluded.title, is_bundle=1, updated_at=excluded.updated_at""",
                (bundle_sku, title, json.dumps(components, separators=(",", ":")), utc_now()),
            )
            await db.execute(
                """INSERT INTO bundles(bundle_sku, component_skus_json, title, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(bundle_sku) DO UPDATE SET component_skus_json=excluded.component_skus_json, title=excluded.title, updated_at=excluded.updated_at""",
                (bundle_sku, json.dumps(components), title, utc_now()),
            )
            await _refresh_bundle_stock_sqlite(db, bundle_sku)
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def _refresh_bundle_stock_pg(conn: Any, bundle_sku: str) -> tuple[int, str]:
    bundle = await conn.fetchrow("SELECT component_skus_json FROM bundles WHERE bundle_sku = $1", bundle_sku)
    if not bundle:
        raise DatabaseError(f"Bundle {bundle_sku!r} does not exist.")
    components = [str(item) for item in bundle["component_skus_json"]]
    rows = await conn.fetch("SELECT sku, stock FROM products WHERE sku = ANY($1::text[])", components)
    stock_map = {str(row["sku"]): int(row["stock"]) for row in rows}
    stock = min((stock_map.get(component, 0) for component in components), default=0)
    status = "active" if stock > 0 else "paused"
    await conn.execute("UPDATE products SET stock=$1, status=$2, updated_at=NOW() WHERE sku=$3", stock, status, bundle_sku)
    return stock, status


async def _refresh_bundle_stock_sqlite(conn: aiosqlite.Connection, bundle_sku: str) -> tuple[int, str]:
    cursor = await conn.execute("SELECT component_skus_json FROM bundles WHERE bundle_sku = ?", (bundle_sku,))
    bundle = await cursor.fetchone()
    await cursor.close()
    if not bundle:
        raise DatabaseError(f"Bundle {bundle_sku!r} does not exist.")
    components = [str(item) for item in json.loads(str(bundle["component_skus_json"]))]
    placeholders = ",".join("?" for _ in components)
    cursor = await conn.execute(f"SELECT sku, stock FROM products WHERE sku IN ({placeholders})", tuple(components))
    rows = await cursor.fetchall()
    await cursor.close()
    stock_map = {str(row["sku"]): int(row["stock"]) for row in rows}
    stock = min((stock_map.get(component, 0) for component in components), default=0)
    status = "active" if stock > 0 else "paused"
    await conn.execute("UPDATE products SET stock=?, status=?, updated_at=? WHERE sku=?", (stock, status, utc_now(), bundle_sku))
    return stock, status


async def refresh_all_bundle_stocks() -> None:
    """Recalculate every bundle under one backend-specific write transaction."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch("SELECT bundle_sku FROM bundles")
                for row in rows:
                    await _refresh_bundle_stock_pg(conn, str(row["bundle_sku"]))
        return
    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("SELECT bundle_sku FROM bundles")
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                await _refresh_bundle_stock_sqlite(db, str(row["bundle_sku"]))
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def create_order(*, order_id: str, sku: str, quantity: int, marketplace: str, customer_reference: str = "") -> None:
    """Persist an idempotent incoming order in received state."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO orders(order_id, sku, quantity, marketplace, customer_reference, created_at, updated_at)
                   VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
                   ON CONFLICT(order_id) DO UPDATE SET quantity=EXCLUDED.quantity, marketplace=EXCLUDED.marketplace,
                     customer_reference=EXCLUDED.customer_reference, updated_at=NOW()""",
                order_id, sku, quantity, marketplace, customer_reference,
            )
        return
    now = utc_now()
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO orders(order_id, sku, quantity, marketplace, customer_reference, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(order_id) DO UPDATE SET quantity=excluded.quantity, marketplace=excluded.marketplace,
                 customer_reference=excluded.customer_reference, updated_at=excluded.updated_at""",
            (order_id, sku, quantity, marketplace, customer_reference, now, now),
        )
        await db.commit()


async def reserve_order_stock(order_id: str, sku: str, quantity: int) -> None:
    """Atomically reserve product or all bundle component stock for an order."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow("SELECT status FROM orders WHERE order_id=$1 FOR UPDATE", order_id)
                if existing and existing["status"] in {"reserved", "dispatching", "dispatched"}:
                    return
                product = await conn.fetchrow("SELECT sku, stock, is_bundle, status FROM products WHERE sku=$1 FOR UPDATE", sku)
                if not product or product["status"] != "active" or int(product["stock"]) < quantity:
                    raise InsufficientStockError(f"Insufficient active stock for {sku}.")
                if product["is_bundle"]:
                    bundle = await conn.fetchrow("SELECT component_skus_json FROM bundles WHERE bundle_sku=$1", sku)
                    if not bundle:
                        raise DatabaseError(f"Bundle metadata missing for {sku}.")
                    for component in [str(item) for item in bundle["component_skus_json"]]:
                        changed = await conn.fetchval(
                            """UPDATE products SET stock=stock-$1, updated_at=NOW()
                               WHERE sku=$2 AND is_bundle=FALSE AND status='active' AND stock >= $1 RETURNING 1""",
                            quantity, component,
                        )
                        if changed != 1:
                            raise InsufficientStockError(f"Insufficient component stock for {component}.")
                else:
                    changed = await conn.fetchval(
                        "UPDATE products SET stock=stock-$1, updated_at=NOW() WHERE sku=$2 AND status='active' AND stock >= $1 RETURNING 1",
                        quantity, sku,
                    )
                    if changed != 1:
                        raise InsufficientStockError(f"Insufficient stock for {sku}.")
                for row in await conn.fetch("SELECT bundle_sku FROM bundles"):
                    await _refresh_bundle_stock_pg(conn, str(row["bundle_sku"]))
                await conn.execute("UPDATE orders SET status='reserved', last_error='', updated_at=NOW() WHERE order_id=$1", order_id)
        return

    async with get_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("SELECT status FROM orders WHERE order_id=?", (order_id,))
            existing = await cursor.fetchone()
            await cursor.close()
            if existing and existing["status"] in {"reserved", "dispatching", "dispatched"}:
                await db.commit()
                return
            cursor = await db.execute("SELECT sku, stock, is_bundle, status FROM products WHERE sku=?", (sku,))
            product = await cursor.fetchone()
            await cursor.close()
            if not product or product["status"] != "active" or int(product["stock"]) < quantity:
                raise InsufficientStockError(f"Insufficient active stock for {sku}.")
            components: list[str] = [sku]
            if product["is_bundle"]:
                cursor = await db.execute("SELECT component_skus_json FROM bundles WHERE bundle_sku=?", (sku,))
                bundle = await cursor.fetchone()
                await cursor.close()
                if not bundle:
                    raise DatabaseError(f"Bundle metadata missing for {sku}.")
                components = [str(item) for item in json.loads(str(bundle["component_skus_json"]))]
            for component in components:
                cursor = await db.execute(
                    """UPDATE products SET stock=stock-?, updated_at=?
                       WHERE sku=? AND status='active' AND stock >= ? AND (is_bundle=0 OR ?=?)""",
                    (quantity, utc_now(), component, quantity, component, sku),
                )
                changed = cursor.rowcount
                await cursor.close()
                if changed != 1:
                    raise InsufficientStockError(f"Insufficient component stock for {component}.")
            cursor = await db.execute("SELECT bundle_sku FROM bundles")
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                await _refresh_bundle_stock_sqlite(db, str(row["bundle_sku"]))
            await db.execute("UPDATE orders SET status='reserved', last_error='', updated_at=? WHERE order_id=?", (utc_now(), order_id))
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def update_order_status(order_id: str, status: str, *, tracking_number: str = "", tracking_url: str = "", error: str = "") -> None:
    """Update one order with fulfillment status, tracking data or error."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE orders SET status=$1, tracking_number=$2, tracking_url=$3, last_error=$4, updated_at=NOW() WHERE order_id=$5", status, tracking_number, tracking_url, error, order_id)
        return
    async with get_connection() as db:
        await db.execute("UPDATE orders SET status=?, tracking_number=?, tracking_url=?, last_error=?, updated_at=? WHERE order_id=?", (status, tracking_number, tracking_url, error, utc_now(), order_id))
        await db.commit()


async def list_orders(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Return orders with bounded pagination."""
    safe_limit, safe_offset = min(max(limit, 1), 500), max(offset, 0)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM orders ORDER BY created_at DESC, order_id ASC LIMIT $1 OFFSET $2", safe_limit, safe_offset)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM orders ORDER BY created_at DESC, order_id ASC LIMIT ? OFFSET ?", (safe_limit, safe_offset))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def count_dashboard_metrics() -> dict[str, int]:
    """Return compact dashboard counters."""
    query = """SELECT
        (SELECT COUNT(*) FROM products) AS product_count,
        (SELECT COUNT(*) FROM products WHERE status='active') AS active_product_count,
        (SELECT COUNT(*) FROM orders WHERE status IN ('received', 'reserved', 'dispatching')) AS pending_order_count,
        (SELECT COUNT(*) FROM system_logs WHERE level='ERROR' AND created_at >= %s) AS error_count"""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query % "NOW() - INTERVAL '24 hours'")
        return {key: int(value) for key, value in dict(row).items()} if row else {}
    async with get_connection() as db:
        cursor = await db.execute(query % "datetime('now', '-24 hours')")
        row = await cursor.fetchone()
        await cursor.close()
        return {key: int(row[key]) for key in row.keys()} if row else {}


async def start_import_run(source: str) -> int:
    """Create an import-run record and return its id."""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            return int(await conn.fetchval("INSERT INTO import_runs(source) VALUES ($1) RETURNING id", source))
    async with get_connection() as db:
        cursor = await db.execute("INSERT INTO import_runs(source, started_at) VALUES (?, ?)", (source, utc_now()))
        await db.commit()
        run_id = int(cursor.lastrowid or 0)
        await cursor.close()
        return run_id


async def finish_import_run(run_id: int, *, imported: int, updated: int, errors: int, error_message: str = "") -> None:
    """Complete an import-run record."""
    status = "success" if errors == 0 else "failed"
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE import_runs SET finished_at=NOW(), imported=$1, updated=$2, errors=$3, status=$4, error_message=$5 WHERE id=$6", imported, updated, errors, status, error_message[:2000], run_id)
        return
    async with get_connection() as db:
        await db.execute("UPDATE import_runs SET finished_at=?, imported=?, updated=?, errors=?, status=?, error_message=? WHERE id=?", (utc_now(), imported, updated, errors, status, error_message[:2000], run_id))
        await db.commit()


async def recent_import_runs(limit: int = 10) -> list[dict[str, Any]]:
    """Return recent import-run records."""
    safe_limit = min(max(limit, 1), 50)
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM import_runs ORDER BY id DESC LIMIT $1", safe_limit)
        return [dict(row) for row in rows]
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM import_runs ORDER BY id DESC LIMIT ?", (safe_limit,))
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]
