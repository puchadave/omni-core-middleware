"""SQLite persistence layer for Omni-Core E-Commerce Middleware.

Provides WAL-mode configured aiosqlite connections with proper type hints,
connection lifecycle management, and table schemas for the application
stack: products and settings.
"""

from __future__ import annotations

import aiosqlite
from pathlib import Path
from typing import Any

DB_PATH = Path("middleware.db")


async def init_db() -> None:
    """Initialize the SQLite database with WAL mode and all required tables.

    Creates tables if they do not exist. Must be called once at application
    startup via the FastAPI lifespan context.
    """
    async with aiosqlite.connect(str(DB_PATH)) as db:
        # WAL mode for maximum concurrent read performance with minimal RAM
        await db.execute("PRAGMA journal_mode=WAL;")
        # Set busy timeout to handle concurrent access gracefully
        await db.execute("PRAGMA busy_timeout=5000;")

        await db.execute(
            """CREATE TABLE IF NOT EXISTS products (
                sku TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT,
                supplier_price REAL DEFAULT 0.0,
                stock INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active'
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )"""
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