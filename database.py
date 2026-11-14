import aiosqlite

DB_PATH = "middleware.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                sku TEXT PRIMARY KEY, title TEXT, category TEXT, 
                supplier_price REAL, stock INTEGER, is_bundle INTEGER DEFAULT 0, status TEXT DEFAULT 'active'
            )
        """)
        await db.execute("CREATE TABLE IF NOT EXISTS bundles (bundle_sku TEXT PRIMARY KEY, component_skus TEXT, title TEXT)")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('omniroute_url', 'http://localhost:4000/v1')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('omniroute_model', 'gemini-2.5-flash')")
        await db.commit()
