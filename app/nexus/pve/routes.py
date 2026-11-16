"""API routes for Proxmox VE node agent: auth, telemetry, action queue, AI planning."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Request

from app.nexus.event_bus.store import NexusEventStore, nexus_event

# Mounted under /api/nexus via nexus router as prefix /pve -> /api/nexus/pve/*
router = APIRouter(prefix="/pve", tags=["nexus-pve"])
store = NexusEventStore()

PVE_TOKEN_HEADER = "X-PVE-Agent-Token"

# In-memory action queue (simple; nexus_events provides audit trail). Per-node queues.
_ACTIONS: dict[str, list[dict[str, Any]]] = {}
_ACTION_RESULTS: dict[str, dict[str, Any]] = {}


def _expected_token() -> str:
    return (os.getenv("PVE_AGENT_TOKEN") or os.getenv("INTERNAL_API_KEY") or "").strip()


def _check_agent_auth(token: str | None, authorization: str | None) -> str:
    expected = _expected_token()
    if not expected:
        raise HTTPException(status_code=503, detail="PVE agent auth not configured (set PVE_AGENT_TOKEN or INTERNAL_API_KEY)")
    provided = (token or "").strip()
    if not provided and authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid PVE agent token")
    return "pve-agent:" + hashlib.sha256(provided.encode()).hexdigest()[:12]


@router.post("/report")
async def pve_report(
    request: Request,
    payload: dict[str, Any] = Body(...),
    x_pve_agent_token: str | None = Header(default=None, alias=PVE_TOKEN_HEADER),
    authorization: str | None = Header(default=None),
):
    """Ingest telemetry/heartbeat from a PVE node agent."""
    agent_id = _check_agent_auth(x_pve_agent_token, authorization)
    await store.ensure_schema()
    node = str(payload.get("node") or payload.get("hostname") or payload.get("nodename") or "unknown")
    ts = payload.get("timestamp") or datetime.now(timezone.utc).isoformat()
    event_payload = {
        "agent_id": agent_id,
        "node": node,
        "hostname": payload.get("hostname") or node,
        "ip": payload.get("ip") or (request.client.host if request.client else ""),
        "pve_version": payload.get("pve_version"),
        "kernel": payload.get("kernel"),
        "uptime_seconds": payload.get("uptime_seconds"),
        "loadavg": payload.get("loadavg"),
        "cpu": payload.get("cpu"),
        "memory": payload.get("memory"),
        "swap": payload.get("swap"),
        "rootfs": payload.get("rootfs"),
        "storages": payload.get("storages") or [],
        "guests": payload.get("guests") or {},
        "agent_version": payload.get("agent_version"),
        "reported_at": ts,
    }
    ev = await nexus_event("PveNodeHeartbeat", event_payload, source="pve-node-agent", entity_type="pve_node", entity_id=node, actor=agent_id)
    return {"ok": True, "node": node, "agent_id": agent_id, "event": ev}


@router.get("/nodes")
async def pve_nodes(limit: int = 200):
    """List latest known PVE node heartbeats."""
    await store.ensure_schema()
    events = await store.list_events(limit=min(max(limit, 1), 500), event_type="PveNodeHeartbeat")
    latest: dict[str, dict[str, Any]] = {}
    for e in events:
        p = e.get("payload_json") or {}
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                p = {}
        node = str(p.get("node") or e.get("entity_id") or "unknown")
        if node not in latest:
            latest[node] = {
                "node": node, "hostname": p.get("hostname"), "ip": p.get("ip"),
                "pve_version": p.get("pve_version"), "kernel": p.get("kernel"),
                "uptime_seconds": p.get("uptime_seconds"), "loadavg": p.get("loadavg"),
                "cpu": p.get("cpu"), "memory": p.get("memory"), "rootfs": p.get("rootfs"),
                "guests": p.get("guests"), "agent_version": p.get("agent_version"),
                "reported_at": p.get("reported_at") or e.get("occurred_at"), "seq": e.get("seq"),
            }
    return {"ok": True, "count": len(latest), "nodes": sorted(latest.values(), key=lambda x: x.get("node") or "")}


@router.post("/actions")
async def pve_queue_action(payload: dict[str, Any] = Body(...)):
    """Queue an action for a PVE node agent (install/tune/fix/backup/script/plan-exec)."""
    await store.ensure_schema()
    node = str(payload.get("node") or "*")
    action = {
        "id": str(uuid.uuid4()),
        "node": node,
        "type": str(payload.get("type") or "noop"),
        "params": payload.get("params") or {},
        "requested_by": "nexus-ui",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "queued",
    }
    _ACTIONS.setdefault(node, []).append(action)
    await nexus_event("PveActionQueued", action, source="nexus-pve", entity_type="pve_node", entity_id=node)
    return {"ok": True, "action": action}


@router.get("/actions/poll")
async def pve_poll_actions(
    node: str,
    x_pve_agent_token: str | None = Header(default=None, alias=PVE_TOKEN_HEADER),
    authorization: str | None = Header(default=None),
):
    """Agent polls for queued actions for its node."""
    agent_id = _check_agent_auth(x_pve_agent_token, authorization)
    items = []
    for key in (node, "*"):
        q = _ACTIONS.get(key, [])
        while q:
            a = q.pop(0)
            a["status"] = "dispatched"
            a["agent_id"] = agent_id
            items.append(a)
    return {"ok": True, "node": node, "actions": items}


@router.post("/actions/result")
async def pve_action_result(
    payload: dict[str, Any] = Body(...),
    x_pve_agent_token: str | None = Header(default=None, alias=PVE_TOKEN_HEADER),
    authorization: str | None = Header(default=None),
):
    agent_id = _check_agent_auth(x_pve_agent_token, authorization)
    await store.ensure_schema()
    action_id = str(payload.get("action_id") or "")
    result = {
        "action_id": action_id,
        "node": payload.get("node"),
        "ok": bool(payload.get("ok")),
        "output": payload.get("output"),
        "error": payload.get("error"),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": agent_id,
    }
    if action_id:
        _ACTION_RESULTS[action_id] = result
    await nexus_event("PveActionResult", result, source="pve-node-agent", entity_type="pve_action", entity_id=action_id or "unknown", actor=agent_id)
    return {"ok": True}


@router.get("/actions/results")
async def pve_action_results(limit: int = 50):
    await store.ensure_schema()
    events = await store.list_events(limit=min(max(limit, 1), 200), event_type="PveActionResult")
    return {"ok": True, "events": events}


@router.post("/plan")
async def pve_ai_plan(payload: dict[str, Any] = Body(...)):
    """Generate an AI-assisted server plan (OmniRoute-connected when available)."""
    from app.nexus.pve.proxmox_manager import generate_server_plan
    requirements = str(payload.get("requirements") or payload.get("prompt") or "").strip()
    target = str(payload.get("target") or "lxc")
    hostname = payload.get("hostname")
    return await generate_server_plan(requirements, target=target, hostname=hostname, context=payload.get("context") or {})


@router.post("/chat")
async def pve_manager_chat(payload: dict[str, Any] = Body(...)):
    """Proxmox Manager AI chat: plans, quick tasks/fixes, guided install actions."""
    from app.nexus.pve.proxmox_manager import manager_chat
    message = str(payload.get("message") or payload.get("prompt") or "").strip()
    node = payload.get("node")
    return await manager_chat(message, node=node, context=payload.get("context") or {})


@router.get("/quick-tasks")
async def pve_quick_tasks():
    from app.nexus.pve.proxmox_manager import QUICK_TASKS
    return {"ok": True, "tasks": QUICK_TASKS}


@router.post("/network/plan")
async def pve_network_plan(payload: dict[str, Any] = Body(...)):
    """AI-assisted network concept (bridges/VLANs/IP plan) for Proxmox nodes."""
    from app.nexus.pve.proxmox_manager import generate_network_plan
    requirements = str(payload.get("requirements") or payload.get("prompt") or "").strip()
    node = str(payload.get("node") or "*")
    return await generate_network_plan(requirements, node=node, context=payload.get("context") or {})


@router.post("/firewall/plan")
async def pve_firewall_plan(payload: dict[str, Any] = Body(...)):
    """AI-assisted pve-firewall policy/rules plan."""
    from app.nexus.pve.proxmox_manager import generate_firewall_plan
    requirements = str(payload.get("requirements") or payload.get("prompt") or "").strip()
    node = str(payload.get("node") or "*")
    scope = str(payload.get("scope") or "cluster")
    return await generate_firewall_plan(requirements, node=node, scope=scope, context=payload.get("context") or {})


@router.post("/firewall/apply")
async def pve_firewall_apply(payload: dict[str, Any] = Body(...)):
    """Queue firewall automation action for node agent (pve-firewall commands/config)."""
    await store.ensure_schema()
    node = str(payload.get("node") or "*")
    action = {
        "id": str(uuid.uuid4()),
        "node": node,
        "type": "apply_firewall",
        "params": {
            "scope": payload.get("scope") or "cluster",
            "commands": payload.get("commands") or [],
            "rules": payload.get("rules") or [],
            "enable": payload.get("enable"),
            "dry_run": bool(payload.get("dry_run", False)),
        },
        "requested_by": "proxmox-manager",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "queued",
    }
    _ACTIONS.setdefault(node, []).append(action)
    await nexus_event("PveActionQueued", action, source="nexus-pve", entity_type="pve_node", entity_id=node)
    return {"ok": True, "action": action}


@router.post("/network/apply")
async def pve_network_apply(payload: dict[str, Any] = Body(...)):
    """Queue network/VLAN configuration action for node agent."""
    await store.ensure_schema()
    node = str(payload.get("node") or "*")
    action = {
        "id": str(uuid.uuid4()),
        "node": node,
        "type": "configure_network",
        "params": {
            "interfaces_snippet": payload.get("interfaces_snippet") or "",
            "commands": payload.get("commands") or [],
            "dry_run": bool(payload.get("dry_run", True)),
            "apply_ifreload": bool(payload.get("apply_ifreload", False)),
        },
        "requested_by": "proxmox-manager",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "queued",
    }
    _ACTIONS.setdefault(node, []).append(action)
    await nexus_event("PveActionQueued", action, source="nexus-pve", entity_type="pve_node", entity_id=node)
    return {"ok": True, "action": action}
