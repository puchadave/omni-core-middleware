"""Asynchronous SQLite initialization and database models for Omni-Core Middleware.

Provides WAL-mode configured aiosqlite connections with proper type hints,
connection lifecycle management, and table schemas for the full application
stack: settings, products, bundles, orders, and logs.
"""

from __future__ import annotations

import aiosqlite
from pathlib import Path
from typing import Any

DB_PATH = Path("middleware.db")


async def init_db() -> None:
    """Initialize the SQLite database with WAL mode and all required tables.

    Creates tables if they do not exist and seeds default settings.
    Must be called once at application startup via the FastAPI lifespan context.
    """
    async with aiosqlite.connect(str(DB_PATH)) as db:
        # WAL mode for maximum concurrent read performance with minimal RAM
        await db.execute("PRAGMA journal_mode=WAL;")
        # Set busy timeout to handle concurrent access gracefully
        await db.execute("PRAGMA busy_timeout=5000;")

        await db.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS products (
                sku TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT,
                supplier_price REAL DEFAULT 0.0,
                stock INTEGER DEFAULT 0,
                is_bundle INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active'
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS bundles (
                bundle_sku TEXT PRIMARY KEY,
                component_skus TEXT,
                title TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                sku TEXT,
                quantity INTEGER DEFAULT 1,
                marketplace VARCHAR(50) DEFAULT 'unknown',
                status TEXT DEFAULT 'pending',
                payment_status TEXT DEFAULT 'pending'
                    CHECK (payment_status IN ('pending', 'paid', 'failed', 'refunded')),
                fulfillment_locked BOOLEAN DEFAULT true,
                created_at TIMESTAMP DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'NOW')),
                updated_at TIMESTAMP DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'NOW'))
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'NOW'))
            )"""
        )

        # Index for performante Payment-Status-Abfragen durch den Worker
        await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_payment_status ON orders(payment_status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_fulfillment_locked ON orders(fulfillment_locked)")

        # Seed default OmniRoute configuration if not present
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('omniroute_url', 'http://localhost:4000/v1')"
        )
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('omniroute_model', 'gemini-2.5-flash')"
        )
        await db.commit()


async def get_connection() -> aiosqlite.Connection:
    """Return a new aiosqlite connection pre-configured for WAL mode.

    Intended for use by internal services that need direct cursor access.
    The caller is responsible for closing the connection.
    """
    conn = await aiosqlite.connect(str(DB_PATH))
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA busy_timeout=5000;")
    return conn


async def close_connection(conn: aiosqlite.Connection) -> None:
    """Safely close an aiosqlite connection."""
    await conn.close()