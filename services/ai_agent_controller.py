"""Natural-language Admin & Business Copilot for Omni-Core."""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field
from database import get_connection, get_pg_pool, get_setting, set_setting, uses_postgres, write_log


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AgentChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    conversation_id: str | None = Field(default=None, max_length=128)


class AgentConfirmRequest(BaseModel):
    action_id: str = Field(min_length=8, max_length=128)


class ChannelConfigInput(BaseModel):
    channel: Literal["shopware", "amazon", "ebay", "email", "messenger"]
    operation: Literal["set", "remove"]
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(default="", max_length=4096)


class BusinessRuleInput(BaseModel):
    rule_key: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=4096)
    description: str = Field(default="", max_length=1000)


class CalendarInput(BaseModel):
    action: Literal["create_event", "update_event", "create_message_draft"]
    title: str = Field(default="", max_length=200)
    start_at: str = Field(default="", max_length=64)
    end_at: str = Field(default="", max_length=64)
    customer_reference: str = Field(default="", max_length=256)
    body: str = Field(default="", max_length=12000)
    event_id: str = Field(default="", max_length=128)


class PendingAction(BaseModel):
    action_id: str
    tool_name: str
    summary: str
    diff: dict[str, Any]
    requiresConfirmation: bool = True


TOOL_DEFINITIONS = [
    {"type": "function", "function": {
        "name": "manage_channel_config",
        "description": "Prepare an allowed channel configuration change for Shopware, Amazon, eBay, email or messenger. Never modify security or fulfillment invariants.",
        "parameters": {"type": "object", "properties": {
            "channel": {"type": "string", "enum": ["shopware", "amazon", "ebay", "email", "messenger"]},
            "operation": {"type": "string", "enum": ["set", "remove"]},
            "key": {"type": "string"}, "value": {"type": "string"}
        }, "required": ["channel", "operation", "key", "value"], "additionalProperties": False}
    }},
    {"type": "function", "function": {
        "name": "manage_business_rules",
        "description": "Prepare a business rule, autoresponder or notification change. Protected payment/fulfillment rules are immutable.",
        "parameters": {"type": "object", "properties": {
            "rule_key": {"type": "string"}, "value": {"type": "string"}, "description": {"type": "string"}
        }, "required": ["rule_key", "value"], "additionalProperties": False}
    }},
    {"type": "function", "function": {
        "name": "crm_and_calendar_actions",
        "description": "Prepare a CRM/calendar event change or customer message draft. Every write requires explicit confirmation.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create_event", "update_event", "create_message_draft"]},
            "title": {"type": "string"}, "start_at": {"type": "string"}, "end_at": {"type": "string"},
            "customer_reference": {"type": "string"}, "body": {"type": "string"}, "event_id": {"type": "string"}
        }, "required": ["action"], "additionalProperties": False}
    }},
    {"type": "function", "function": {
        "name": "system_diagnostics",
        "description": "Read-only diagnostics for orders, payment status, fulfillment locks and tracking.",
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "string", "enum": ["orders", "payment", "tracking"]},
            "order_id": {"type": "string"}
        }, "required": ["scope"], "additionalProperties": False}
    }}
]

PROTECTED_RULE_KEYS = {
    "fulfillment.payment_required",
    "fulfillment.unpaid_order_block",
    "fulfillment.payment_status_gate",
    "fulfillment_locked",
}


def _masked(value: str) -> str:
    if len(value) <= 4:
        return "****"
    return value[:2] + "****" + value[-2:]


async def ensure_agent_schema() -> None:
    pg = (
        """CREATE TABLE IF NOT EXISTS ai_agent_pending_actions (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL DEFAULT '', tool_name TEXT NOT NULL,
            payload_json JSONB NOT NULL DEFAULT '{}'::jsonb, summary TEXT NOT NULL,
            diff_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','cancelled','expired')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), confirmed_at TIMESTAMPTZ)""",
        """CREATE TABLE IF NOT EXISTS ai_agent_audit_log (
            id BIGSERIAL PRIMARY KEY, action_id TEXT NOT NULL, actor TEXT NOT NULL, prompt TEXT NOT NULL,
            tool_name TEXT NOT NULL, action_payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            result_json JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS crm_calendar_events (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL DEFAULT '',
            customer_reference TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS crm_message_drafts (
            id TEXT PRIMARY KEY, customer_reference TEXT NOT NULL DEFAULT '', body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    )
    sqlite = (
        """CREATE TABLE IF NOT EXISTS ai_agent_pending_actions (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL DEFAULT '', tool_name TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}', summary TEXT NOT NULL,
            diff_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','cancelled','expired')),
            created_at TEXT NOT NULL, confirmed_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS ai_agent_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, action_id TEXT NOT NULL, actor TEXT NOT NULL, prompt TEXT NOT NULL,
            tool_name TEXT NOT NULL, action_payload_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS crm_calendar_events (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL DEFAULT '',
            customer_reference TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS crm_message_drafts (
            id TEXT PRIMARY KEY, customer_reference TEXT NOT NULL DEFAULT '', body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    )
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            for statement in pg:
                await conn.execute(statement)
    else:
        async with get_connection() as db:
            for statement in sqlite:
                await db.execute(statement)
            await db.commit()


async def _save_pending(action: PendingAction, payload: dict[str, Any], conversation_id: str) -> None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ai_agent_pending_actions(id,conversation_id,tool_name,payload_json,summary,diff_json) VALUES ($1,$2,$3,$4::jsonb,$5,$6::jsonb)",
                action.action_id, conversation_id, action.tool_name, json.dumps(payload), action.summary, json.dumps(action.diff))
        return
    async with get_connection() as db:
        await db.execute(
            "INSERT INTO ai_agent_pending_actions(id,conversation_id,tool_name,payload_json,summary,diff_json,created_at) VALUES (?,?,?,?,?,?,?)",
            (action.action_id, conversation_id, action.tool_name, json.dumps(payload), action.summary, json.dumps(action.diff), utc_now()))
        await db.commit()


async def _get_pending(action_id: str) -> dict[str, Any] | None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM ai_agent_pending_actions WHERE id=$1 AND status='pending'", action_id)
        return dict(row) if row else None
    async with get_connection() as db:
        cursor = await db.execute("SELECT * FROM ai_agent_pending_actions WHERE id=? AND status='pending'", (action_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None


async def _mark_action(action_id: str, status: str) -> None:
    now = utc_now()
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE ai_agent_pending_actions SET status=$1, confirmed_at=NOW() WHERE id=$2", status, action_id)
        return
    async with get_connection() as db:
        await db.execute("UPDATE ai_agent_pending_actions SET status=?, confirmed_at=? WHERE id=?", (status, now, action_id))
        await db.commit()


async def _audit(action_id: str, actor: str, prompt: str, tool_name: str, payload: dict[str, Any], result: dict[str, Any]) -> None:
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ai_agent_audit_log(action_id,actor,prompt,tool_name,action_payload_json,result_json) VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb)",
                action_id, actor[:256], prompt[:12000], tool_name, json.dumps(payload), json.dumps(result))
        return
    async with get_connection() as db:
        await db.execute(
            "INSERT INTO ai_agent_audit_log(action_id,actor,prompt,tool_name,action_payload_json,result_json,created_at) VALUES (?,?,?,?,?,?,?)",
            (action_id, actor[:256], prompt[:12000], tool_name, json.dumps(payload), json.dumps(result), utc_now()))
        await db.commit()


async def _diagnostics(args: dict[str, Any]) -> dict[str, Any]:
    order_id = args.get("order_id", "")
    query = "SELECT order_id,sku,quantity,marketplace,payment_status,fulfillment_locked,status,tracking_number,tracking_url,created_at,updated_at FROM orders"
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if order_id:
                row = await conn.fetchrow(query + " WHERE order_id=$1", order_id)
                return {"scope": args["scope"], "order": dict(row) if row else None}
            rows = await conn.fetch(query + " ORDER BY created_at DESC LIMIT 25")
        return {"scope": args["scope"], "orders": [dict(r) for r in rows]}
    async with get_connection() as db:
        if order_id:
            cursor = await db.execute(query + " WHERE order_id=?", (order_id,))
        else:
            cursor = await db.execute(query + " ORDER BY created_at DESC LIMIT 25")
        rows = await cursor.fetchall()
        await cursor.close()
        return {"scope": args["scope"], "orders": [dict(r) for r in rows]}


async def execute_tool_call(name: str, args: dict[str, Any], conversation_id: str, prompt: str = "") -> dict[str, Any]:
    if name == "system_diagnostics":
        return {"readOnly": True, "result": await _diagnostics(args)}
    if name == "manage_channel_config":
        payload = ChannelConfigInput.model_validate(args).model_dump()
        normalized_key = payload["key"].lower()
        if payload["key"] in PROTECTED_RULE_KEYS or normalized_key.startswith(("fulfillment", "payment_status")):
            raise ValueError("Protected fulfillment/payment security controls cannot be modified by the AI agent.")
        action = PendingAction(
            action_id=secrets.token_urlsafe(18),
            tool_name=name,
            summary=f"{payload['channel']} Konfiguration {payload['operation']}: {payload['key']}",
            diff={"channel": payload["channel"], "key": payload["key"], "operation": payload["operation"],
                  "new_value": _masked(payload["value"]) if any(x in normalized_key for x in ("key","token","secret","password")) else payload["value"]},
        )
        payload["_prompt"] = prompt
        await _save_pending(action, payload, conversation_id)
        return {"readOnly": False, "requiresConfirmation": True, "pendingAction": action.model_dump()}
    if name == "manage_business_rules":
        payload = BusinessRuleInput.model_validate(args).model_dump()
        if payload["rule_key"] in PROTECTED_RULE_KEYS or payload["rule_key"].lower().startswith(("fulfillment", "payment_status")):
            raise ValueError("Die Fulfillment-Sperre für unbezahlte Bestellungen ist unveränderbar.")
        action = PendingAction(
            action_id=secrets.token_urlsafe(18),
            tool_name=name,
            summary=f"Geschäftsregel ändern: {payload['rule_key']}",
            diff={"rule_key": payload["rule_key"], "new_value": payload["value"], "description": payload["description"]},
        )
        payload["_prompt"] = prompt
        await _save_pending(action, payload, conversation_id)
        return {"readOnly": False, "requiresConfirmation": True, "pendingAction": action.model_dump()}
    if name == "crm_and_calendar_actions":
        payload = CalendarInput.model_validate(args).model_dump()
        action = PendingAction(
            action_id=secrets.token_urlsafe(18),
            tool_name=name,
            summary=f"CRM/Kalender-Aktion: {payload['action']}",
            diff={k: v for k, v in payload.items() if v},
        )
        payload["_prompt"] = prompt
        await _save_pending(action, payload, conversation_id)
        return {"readOnly": False, "requiresConfirmation": True, "pendingAction": action.model_dump()}
    raise ValueError(f"Tool not allowed: {name}")


async def _execute_calendar(payload: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    action = payload["action"]
    if action == "create_message_draft":
        draft_id = str(uuid.uuid4())
        if uses_postgres():
            pool = await get_pg_pool()
            async with pool.acquire() as conn:
                await conn.execute("INSERT INTO crm_message_drafts(id,customer_reference,body,created_at,updated_at) VALUES ($1,$2,$3,NOW(),NOW())",
                                   draft_id, payload["customer_reference"], payload["body"])
        else:
            async with get_connection() as db:
                await db.execute("INSERT INTO crm_message_drafts(id,customer_reference,body,created_at,updated_at) VALUES (?,?,?,?,?)",
                                 (draft_id, payload["customer_reference"], payload["body"], now, now))
                await db.commit()
        return {"status": "created", "draft_id": draft_id}
    event_id = payload.get("event_id") or str(uuid.uuid4())
    if uses_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if action == "update_event":
                await conn.execute("UPDATE crm_calendar_events SET title=$1,start_at=$2,end_at=$3,customer_reference=$4,body=$5,updated_at=NOW() WHERE id=$6",
                                   payload["title"], payload["start_at"], payload["end_at"], payload["customer_reference"], payload["body"], event_id)
            else:
                await conn.execute("INSERT INTO crm_calendar_events(id,title,start_at,end_at,customer_reference,body,created_at,updated_at) VALUES ($1,$2,$3,$4,$5,$6,NOW(),NOW())",
                                   event_id, payload["title"], payload["start_at"], payload["end_at"], payload["customer_reference"], payload["body"])
    else:
        async with get_connection() as db:
            if action == "update_event":
                await db.execute("UPDATE crm_calendar_events SET title=?,start_at=?,end_at=?,customer_reference=?,body=?,updated_at=? WHERE id=?",
                                 (payload["title"], payload["start_at"], payload["end_at"], payload["customer_reference"], payload["body"], now, event_id))
            else:
                await db.execute("INSERT INTO crm_calendar_events(id,title,start_at,end_at,customer_reference,body,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                                 (event_id, payload["title"], payload["start_at"], payload["end_at"], payload["customer_reference"], payload["body"], now, now))
            await db.commit()
    return {"status": "updated" if action == "update_event" else "created", "event_id": event_id}


async def execute_confirmed(action_id: str, actor: str) -> dict[str, Any]:
    row = await _get_pending(action_id)
    if not row:
        raise ValueError("Aktion nicht gefunden, bereits verarbeitet oder nicht mehr freigegeben.")
    payload = json.loads(row["payload_json"])
    prompt = str(payload.pop("_prompt", ""))
    tool_name = str(row["tool_name"])
    if tool_name == "manage_channel_config":
        key = f"channel.{payload['channel']}.{payload['key']}"
        secret = any(x in payload["key"].lower() for x in ("key","token","secret","password"))
        if payload["operation"] == "remove":
            await set_setting(key, "", is_secret=secret)
            result = {"status": "removed", "key": key}
        else:
            await set_setting(key, payload["value"], is_secret=secret)
            result = {"status": "updated", "key": key}
    elif tool_name == "manage_business_rules":
        await set_setting(f"business_rule.{payload['rule_key']}", payload["value"])
        result = {"status": "updated", "rule_key": payload["rule_key"]}
    elif tool_name == "crm_and_calendar_actions":
        result = await _execute_calendar(payload)
    else:
        raise ValueError(f"Unknown AI agent tool: {tool_name}")
    await _mark_action(action_id, "confirmed")
    await _audit(action_id, actor, prompt, tool_name, payload, result)
    await write_log("INFO", "ai_agent_action_confirmed", f"AI agent action {action_id} confirmed.",
                    {"action_id": action_id, "tool": tool_name, "actor": actor})
    return result


async def chat_with_agent(message: str, conversation_id: str | None, actor: str) -> dict[str, Any]:
    from services.omniroute_client import OmniRouteClient

    base_url = await get_setting("omniroute_url") or await get_setting("omni_route_base_url")
    api_key = await get_setting("omniroute_api_key") or await get_setting("omni_route_api_key")
    if not base_url:
        raise RuntimeError("OmniRoute ist nicht konfiguriert.")
    client = OmniRouteClient(base_url, api_key or "")
    model = await get_setting("omniroute_agent_model") or "gemini-2.5-flash"
    system = (
        "Du bist der sichere Omni-Core Admin & Business Copilot. Nutze ausschließlich die bereitgestellten Tools. "
        "Lies zuerst Daten, bevor du Änderungen vorbereitest. Schreibende Tools erzeugen IMMER eine Bestätigungsvorschau. "
        "Die Fulfillment-Sperre für unbezahlte Bestellungen ist unveränderbar: payment_status != 'paid' muss blockieren. "
        "Keine Shell-, SQL-, Datei-, Netzwerk- oder beliebige Code-Ausführung. Keine Geheimnisse im Chat ausgeben. Antworte präzise auf Deutsch."
    )
    conversation_id = conversation_id or str(uuid.uuid4())
    messages = [{"role": "system", "content": system}, {"role": "user", "content": message}]
    pending: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []

    for _ in range(3):
        result = await client.chat_completion(model=model, messages=messages, tools=TOOL_DEFINITIONS, tool_choice="auto")
        calls = result.get("tool_calls", [])
        if not calls:
            return {"conversation_id": conversation_id, "message": result.get("content") or "Aktion vorbereitet.",
                    "pending_actions": pending, "tool_results": tool_results, "requiresConfirmation": bool(pending)}
        assistant_message = {"role": "assistant", "content": result.get("content") or "", "tool_calls": [
            {"id": c.get("id"), "type": "function", "function": {"name": c.get("name"), "arguments": c.get("arguments", "{}")}}
            for c in calls
        ]}
        messages.append(assistant_message)
        for call in calls:
            name = call.get("name")
            raw_args = call.get("arguments", "{}")
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            execution = await execute_tool_call(name, args, conversation_id, prompt=message)
            tool_results.append({"tool": name, "result": execution})
            if execution.get("pendingAction"):
                pending.append(execution["pendingAction"])
            messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": json.dumps(execution, ensure_ascii=False)})
            if execution.get("pendingAction"):
                return {"conversation_id": conversation_id, "message": result.get("content") or "Änderung vorbereitet. Bitte prüfen und bestätigen.",
                        "pending_actions": pending, "tool_results": tool_results, "requiresConfirmation": True}
    return {"conversation_id": conversation_id, "message": "Die maximale Tool-Ausführungstiefe wurde erreicht.", "pending_actions": pending,
            "tool_results": tool_results, "requiresConfirmation": bool(pending)}
