"""Per-marketplace product content management (separate from canonical product fields).

Stores individual, marketplace-specific content/SEO/status per product so each channel can
be managed independently (title, descriptions, SEO, media refs, keywords, status, notes).
"""

from __future__ import annotations

import json
from typing import Any

from database import get_connection, get_pg_pool, utc_now, uses_postgres, write_log


async def ensure_marketplace_content_table() -> None:
    ddl_pg = """
    CREATE TABLE IF NOT EXISTS product_marketplace_content (
        id TEXT PRIMARY KEY,
        product_sku TEXT NOT NULL,
        marketplace TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        short_description TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        seo_title TEXT NOT NULL DEFAULT '',
        meta_description TEXT NOT NULL DEFAULT '',
        keywords_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        bullets_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        media_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'draft',
        approval_status TEXT NOT NULL DEFAULT 'draft',
        last_generated_at TIMESTAMPTZ,
        metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(product_sku, marketplace)
    )
    """
    ddl_sq = """CREATE TABLE IF NOT EXISTS product_marketplace_content (
        id TEXT PRIMARY KEY, product_sku TEXT NOT NULL, marketplace TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '', short_description TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
        seo_title TEXT NOT NULL DEFAULT '', meta_description TEXT NOT NULL DEFAULT '',
        keywords_json TEXT NOT NULL DEFAULT '[]', bullets_json TEXT NOT NULL DEFAULT '[]', media_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'draft', approval_status TEXT NOT NULL DEFAULT 'draft', last_generated_at TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(product_sku, marketplace)
    )"""
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(ddl_pg)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_pmc_sku ON product_marketplace_content(product_sku)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_pmc_marketplace ON product_marketplace_content(marketplace)")
        return
    async with get_connection() as db:
        await db.execute(ddl_sq)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pmc_sku ON product_marketplace_content(product_sku)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pmc_marketplace ON product_marketplace_content(marketplace)")
        await db.commit()


def _jd(v, default):
    if v is None:
        return json.dumps(default, ensure_ascii=False)
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, default=str)


async def upsert_marketplace_content(data: dict[str, Any]) -> dict[str, Any]:
    await ensure_marketplace_content_table()
    import uuid as _uuid
    sku = str(data.get("product_sku") or "").strip()
    mp = str(data.get("marketplace") or data.get("channel") or "").strip().lower()
    if not sku or not mp:
        raise ValueError("product_sku and marketplace required")
    cid = str(data.get("id") or f"pmc-{sku}-{mp}")
    now = utc_now()
    fields = (
        cid, sku, mp,
        str(data.get("title") or ""), str(data.get("short_description") or ""), str(data.get("description") or ""),
        str(data.get("seo_title") or ""), str(data.get("meta_description") or ""),
        _jd(data.get("keywords") or data.get("keywords_json"), []),
        _jd(data.get("bullets") or data.get("bullets_json"), []),
        _jd(data.get("media") or data.get("media_json"), {}),
        str(data.get("status") or "draft"), str(data.get("approval_status") or "draft"),
        data.get("last_generated_at"),
        _jd(data.get("metadata") or data.get("metadata_json"), {}),
    )
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO product_marketplace_content(id,product_sku,marketplace,title,short_description,description,seo_title,meta_description,keywords_json,bullets_json,media_json,status,approval_status,last_generated_at,metadata_json,created_at,updated_at)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb,$11::jsonb,$12,$13,$14,$15::jsonb,NOW(),NOW())
                   ON CONFLICT(product_sku,marketplace) DO UPDATE SET
                     title=EXCLUDED.title,short_description=EXCLUDED.short_description,description=EXCLUDED.description,
                     seo_title=EXCLUDED.seo_title,meta_description=EXCLUDED.meta_description,
                     keywords_json=EXCLUDED.keywords_json,bullets_json=EXCLUDED.bullets_json,media_json=EXCLUDED.media_json,
                     status=EXCLUDED.status,approval_status=EXCLUDED.approval_status,last_generated_at=EXCLUDED.last_generated_at,
                     metadata_json=EXCLUDED.metadata_json,updated_at=NOW()""",
                *fields)
            row = await conn.fetchrow("SELECT * FROM product_marketplace_content WHERE product_sku=$1 AND marketplace=$2", sku, mp)
        return dict(row) if row else {"id": cid}
    async with get_connection() as db:
        await db.execute(
            """INSERT INTO product_marketplace_content(id,product_sku,marketplace,title,short_description,description,seo_title,meta_description,keywords_json,bullets_json,media_json,status,approval_status,last_generated_at,metadata_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(product_sku,marketplace) DO UPDATE SET
                 title=excluded.title,short_description=excluded.short_description,description=excluded.description,
                 seo_title=excluded.seo_title,meta_description=excluded.meta_description,
                 keywords_json=excluded.keywords_json,bullets_json=excluded.bullets_json,media_json=excluded.media_json,
                 status=excluded.status,approval_status=excluded.approval_status,last_generated_at=excluded.last_generated_at,
                 metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
            (fields[0], fields[1], fields[2], fields[3], fields[4], fields[5], fields[6], fields[7], fields[8], fields[9], fields[10], fields[11], fields[12], fields[13], fields[14], now, now))
        await db.commit()
        cur = await db.execute("SELECT * FROM product_marketplace_content WHERE product_sku=? AND marketplace=?", (sku, mp))
        row = await cur.fetchone(); await cur.close()
        return dict(row) if row else {"id": cid}


async def list_marketplace_content(product_sku: str | None = None, marketplace: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    await ensure_marketplace_content_table()
    limit = min(max(limit, 1), 1000)
    if uses_postgres():
        pool = await get_pg_pool()
        q = "SELECT * FROM product_marketplace_content WHERE 1=1"; args=[]
        if product_sku: args.append(product_sku); q += f" AND product_sku=${len(args)}"
        if marketplace: args.append(marketplace.lower()); q += f" AND marketplace=${len(args)}"
        args.append(limit); q += f" ORDER BY updated_at DESC LIMIT ${len(args)}"
        async with pool.acquire() as conn:
            rows = await conn.fetch(q, *args)
        return [dict(r) for r in rows]
    async with get_connection() as db:
        q = "SELECT * FROM product_marketplace_content WHERE 1=1"; args=[]
        if product_sku: q += " AND product_sku=?"; args.append(product_sku)
        if marketplace: q += " AND marketplace=?"; args.append(marketplace.lower())
        q += " ORDER BY updated_at DESC LIMIT ?"; args.append(limit)
        cur = await db.execute(q, tuple(args)); rows = await cur.fetchall(); await cur.close()
        return [dict(r) for r in rows]


async def get_marketplace_content(product_sku: str, marketplace: str) -> dict[str, Any] | None:
    await ensure_marketplace_content_table()
    mp = marketplace.lower()
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM product_marketplace_content WHERE product_sku=$1 AND marketplace=$2", product_sku, mp)
        return dict(row) if row else None
    async with get_connection() as db:
        cur = await db.execute("SELECT * FROM product_marketplace_content WHERE product_sku=? AND marketplace=?", (product_sku, mp))
        row = await cur.fetchone(); await cur.close()
        return dict(row) if row else None
