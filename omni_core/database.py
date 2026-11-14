import aiosqlite
import os
import logging
from contextlib import asynccontextmanager
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

DATABASE_PATH = os.getenv("DATABASE_PATH", "middleware.db")

async def init_db() -> None:
    """Initializes the SQLite database with WAL mode and required tables."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        # Enable WAL mode for high concurrency and performance
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        
        # Settings Table: Key-Value Store
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Products Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                sku TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT,
                supplier_price REAL NOT NULL,
                stock INTEGER NOT NULL DEFAULT 0,
                is_bundle BOOLEAN DEFAULT 0,
                status TEXT DEFAULT 'active'
            )
        """)

        # Bundles Table: Links bundle SKU to component SKUs
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bundles (
                bundle_sku TEXT PRIMARY KEY,
                component_skus TEXT NOT NULL,
                title TEXT NOT NULL,
                FOREIGN KEY (bundle_sku) REFERENCES products (sku)
            )
        """)

        # Orders Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                sku TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                customer_email TEXT,
                marketplace TEXT,
                status TEXT DEFAULT 'pending',
                tracking_number TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sku) REFERENCES products (sku)
            )
        """)

        # Logs Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT,
                message TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.commit()
        logger.info("Database initialized successfully in WAL mode.")

@asynccontextmanager
async def get_db():
    """Generator for database connection."""
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()

# Helper functions for settings
async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
        await db.commit()

async def get_setting(key: str) -> Optional[str]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None
