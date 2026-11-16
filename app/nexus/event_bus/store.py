"""Immutable ESAA-inspired Nexus event store (append-only) with idempotent append."""
from __future__ import annotations
import json, logging
from typing import Any
from database import get_connection, get_pg_pool, uses_postgres, utc_now
from .models import NexusEvent

logger = logging.getLogger(__name__)

SCHEMA_PG = [
    """
    CREATE TABLE IF NOT EXISTS nexus_events (
        seq BIGSERIAL PRIMARY KEY,
        event_id TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        entity_type TEXT NOT NULL DEFAULT '',
        entity_id TEXT NOT NULL DEFAULT '',
        correlation_id TEXT NOT NULL DEFAULT '',
        trace_id TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL DEFAULT 'system',
        schema_version INTEGER NOT NULL DEFAULT 1,
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        event_hash TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_nexus_events_type_seq ON nexus_events(event_type, seq DESC)",
    "CREATE INDEX IF NOT EXISTS idx_nexus_events_entity ON nexus_events(entity_type, entity_id, seq DESC)",
    "CREATE INDEX IF NOT EXISTS idx_nexus_events_corr ON nexus_events(correlation_id, seq DESC)",
]

SCHEMA_SQLITE = [
    """
    CREATE TABLE IF NOT EXISTS nexus_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        entity_type TEXT NOT NULL DEFAULT '',
        entity_id TEXT NOT NULL DEFAULT '',
        correlation_id TEXT NOT NULL DEFAULT '',
        trace_id TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL DEFAULT 'system',
        schema_version INTEGER NOT NULL DEFAULT 1,
        occurred_at TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        event_hash TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_nexus_events_type_seq ON nexus_events(event_type, seq DESC)",
    "CREATE INDEX IF NOT EXISTS idx_nexus_events_entity ON nexus_events(entity_type, entity_id, seq DESC)",
    "CREATE INDEX IF NOT EXISTS idx_nexus_events_corr ON nexus_events(correlation_id, seq DESC)",
]

class NexusEventStore:
    async def ensure_schema(self) -> None:
        if uses_postgres():
            pool = await get_pg_pool()
            async with pool.acquire() as conn:
                for s in SCHEMA_PG:
                    try: await conn.execute(s)
                    except Exception: logger.debug("nexus schema stmt skipped", exc_info=True)
            return
        async with get_connection() as db:
            for s in SCHEMA_SQLITE:
                try: await db.execute(s)
                except Exception: logger.debug("nexus sqlite stmt skipped", exc_info=True)
            await db.commit()

    async def append(self, event: NexusEvent) -> dict[str, Any]:
        r = event.to_record()
        if uses_postgres():
            pool = await get_pg_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """INSERT INTO nexus_events(event_id,event_type,source,entity_type,entity_id,correlation_id,trace_id,actor,schema_version,occurred_at,payload_json,event_hash)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,NOW(),$10::jsonb,$11)
                       ON CONFLICT(event_id) DO NOTHING RETURNING seq,event_id,event_type,occurred_at""",
                    r["event_id"], r["event_type"], r["source"], r["entity_type"], r["entity_id"], r["correlation_id"], r["trace_id"], r["actor"], r["schema_version"], r["payload_json"], r["event_hash"],
                )
                if row: return dict(row)
                ex = await conn.fetchrow("SELECT seq,event_id,event_type,occurred_at FROM nexus_events WHERE event_id=$1", r["event_id"])
                return dict(ex) if ex else {"event_id": r["event_id"], "deduped": True}
        async with get_connection() as db:
            try:
                cur = await db.execute(
                    """INSERT INTO nexus_events(event_id,event_type,source,entity_type,entity_id,correlation_id,trace_id,actor,schema_version,occurred_at,payload_json,event_hash)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r["event_id"], r["event_type"], r["source"], r["entity_type"], r["entity_id"], r["correlation_id"], r["trace_id"], r["actor"], r["schema_version"], r["occurred_at"], r["payload_json"], r["event_hash"]),
                )
                await db.commit(); seq = cur.lastrowid
                return {"seq": seq, "event_id": r["event_id"], "event_type": r["event_type"], "occurred_at": r["occurred_at"]}
            except Exception:
                cur = await db.execute("SELECT seq,event_id,event_type,occurred_at FROM nexus_events WHERE event_id=?", (r["event_id"],))
                row = await cur.fetchone(); await cur.close()
                return dict(row) if row else {"event_id": r["event_id"], "deduped": True}

    async def list_events(self, limit: int = 100, event_type: str | None = None, entity_id: str | None = None, correlation_id: str | None = None) -> list[dict[str, Any]]:
        limit = min(max(int(limit or 100), 1), 500)
        clauses, params = [], []
        if event_type: clauses.append(("event_type=$%d" if uses_postgres() else "event_type=?")); params.append(event_type)
        if entity_id: clauses.append(("entity_id=$%d" if uses_postgres() else "entity_id=?")); params.append(entity_id)
        if correlation_id: clauses.append(("correlation_id=$%d" if uses_postgres() else "correlation_id=?")); params.append(correlation_id)
        if uses_postgres():
            where = ""
            if clauses:
                numbered = []
                for i,c in enumerate(clauses,1): numbered.append(c.replace("$%d", f"${i}"))
                where = "WHERE " + " AND ".join(numbered)
            q = f"SELECT seq,event_id,event_type,source,entity_type,entity_id,correlation_id,trace_id,actor,schema_version,occurred_at,payload_json,event_hash FROM nexus_events {where} ORDER BY seq DESC LIMIT ${len(params)+1}"
            pool = await get_pg_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(q, *(params+[limit]))
            return [dict(r) for r in rows]
        where = ("WHERE " + " AND ".join([c.replace("$%d","?") for c in clauses])) if clauses else ""
        q = f"SELECT seq,event_id,event_type,source,entity_type,entity_id,correlation_id,trace_id,actor,schema_version,occurred_at,payload_json,event_hash FROM nexus_events {where} ORDER BY seq DESC LIMIT ?"
        async with get_connection() as db:
            cur = await db.execute(q, (*params, limit))
            rows = await cur.fetchall(); await cur.close()
            out=[]
            for r in rows:
                d=dict(r)
                if isinstance(d.get("payload_json"), str):
                    try: d["payload"]=json.loads(d["payload_json"])
                    except Exception: d["payload"]={}
                else: d["payload"]=d.get("payload_json") or {}
                out.append(d)
            return out

    async def stats(self) -> dict[str, Any]:
        if uses_postgres():
            pool = await get_pg_pool()
            async with pool.acquire() as conn:
                total = await conn.fetchval("SELECT COUNT(*) FROM nexus_events")
                by_type = await conn.fetch("SELECT event_type, COUNT(*) c FROM nexus_events GROUP BY event_type ORDER BY c DESC LIMIT 20")
            return {"total": int(total or 0), "by_type": {r["event_type"]: int(r["c"]) for r in by_type}}
        async with get_connection() as db:
            cur = await db.execute("SELECT COUNT(*) c FROM nexus_events"); row=await cur.fetchone(); await cur.close()
            total = int(row["c"]) if row else 0
            cur = await db.execute("SELECT event_type, COUNT(*) c FROM nexus_events GROUP BY event_type ORDER BY c DESC LIMIT 20")
            rows = await cur.fetchall(); await cur.close()
            return {"total": total, "by_type": {r["event_type"]: int(r["c"]) for r in rows}}

_store = NexusEventStore()

async def nexus_event(event_type: str, payload: dict[str, Any], **kw) -> dict[str, Any]:
    try:
        await _store.ensure_schema()
        ev = NexusEvent(event_type=event_type, payload=payload or {}, **kw)
        return await _store.append(ev)
    except Exception as e:
        logger.warning("nexus_event append failed: %s", e)
        return {"ok": False, "error": str(e)}
