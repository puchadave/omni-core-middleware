"""NEXUS Multi-Agent Control UI & API routes."""
from __future__ import annotations
import json, uuid
from typing import Any
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.nexus.event_bus.store import NexusEventStore, nexus_event
from app.nexus.observers.supervisor import nexus_supervisor
from app.nexus.skills.registry import nexus_registry
from app.nexus.skills.erp_guard import skill_erp_guard
from app.nexus.skills.matterhorn_enrich import skill_matterhorn_enrich
from app.nexus.skills.media_studio import skill_media_studio
from app.nexus.skills.shopware_sync import skill_shopware_sync
from app.nexus.skills.social_publish import skill_social_publish
from app.nexus.omnichannel.adapter import nexus_omnichannel
from database import get_connection, uses_postgres, get_pg_pool

router = APIRouter(prefix="/api/nexus", tags=["nexus"])
store = NexusEventStore()

def _register_defaults():
    if nexus_registry.get("skill_matterhorn_enrich"): return
    nexus_registry.register("skill_matterhorn_enrich", skill_matterhorn_enrich, {"desc": "SEO texts & attributes via omni-content-seo + knowledge"})
    nexus_registry.register("skill_media_studio", skill_media_studio, {"desc": "Product image/video briefs via omni-vision-photo/omni-video-gen"})
    nexus_registry.register("skill_shopware_sync", skill_shopware_sync, {"desc": "Shopware 6 bidirectional sync"})
    nexus_registry.register("skill_erp_guard", skill_erp_guard, {"desc": "Payment/fulfillment guard before customer response"})
    nexus_registry.register("skill_social_publish", skill_social_publish, {"desc": "Social commerce captions + publish queue"})
    for a in ["orchestrator","matterhorn_enrich","media_studio","shopware_sync","erp_guard","social_publish","omnichannel","supervisor"]:
        nexus_supervisor.register_agent(a)

@router.on_event("startup")
async def _startup():
    _register_defaults()
    try: await store.ensure_schema()
    except Exception: pass

@router.get("/status")
async def nexus_status():
    _register_defaults(); await store.ensure_schema()
    return {"ok": True, "supervisor": nexus_supervisor.snapshot(), "events": await store.stats(), "skills": nexus_registry.list(), "combos": ["omni-content-seo","omni-vision-photo","omni-video-gen","omni-local-gdpr","omni-crm-support","omni-admin-agent"], "omniroute_base": "http://192.168.176.22:20128/v1"}

@router.get("/events")
async def nexus_events(limit: int = 100, event_type: str | None = None, entity_id: str | None = None, correlation_id: str | None = None):
    _register_defaults(); await store.ensure_schema()
    return {"ok": True, "items": await store.list_events(limit=limit, event_type=event_type, entity_id=entity_id, correlation_id=correlation_id)}

@router.get("/skills")
async def nexus_skills():
    _register_defaults()
    return {"ok": True, "skills": nexus_registry.list()}

@router.post("/skills/execute")
async def nexus_skill_execute(payload: dict[str, Any] = Body(...)):
    _register_defaults(); await store.ensure_schema()
    name = str(payload.get("skill") or "")
    args = payload.get("args") or {}
    cid = str(payload.get("correlation_id") or uuid.uuid4().hex)
    res = await nexus_registry.execute(name, correlation_id=cid, **args)
    await nexus_supervisor.record("orchestrator", name, res.ok, res.latency_ms, error=res.error, correlation_id=cid)
    if not res.ok: raise HTTPException(status_code=400, detail={"error": res.error, "skill": name})
    return {"ok": True, "skill": name, "correlation_id": cid, "result": res.output, "latency_ms": res.latency_ms}

@router.post("/pipeline/product-imported")
async def nexus_product_imported(payload: dict[str, Any] = Body(...)):
    """E2E trigger: ProductImported -> enrich -> shopware sync -> social queue -> event log."""
    _register_defaults(); await store.ensure_schema()
    cid = str(payload.get("correlation_id") or uuid.uuid4().hex)
    product = payload.get("product") or payload
    sku = str(product.get("sku") or product.get("id") or "")
    await nexus_event("ProductImported", {"sku": sku, "source": payload.get("source","matterhorn")}, source="nexus.pipeline", entity_type="product", entity_id=sku, correlation_id=cid)
    r1 = await nexus_registry.execute("skill_matterhorn_enrich", product=product, sku=sku, correlation_id=cid)
    await nexus_supervisor.record("matterhorn_enrich","skill_matterhorn_enrich", r1.ok, r1.latency_ms, correlation_id=cid)
    enriched_product = {**product, **(((r1.output or {}).get("enrichment")) or {})} if isinstance((r1.output or {}).get("enrichment"), dict) else product
    r2 = await nexus_registry.execute("skill_shopware_sync", action="upsert_product", sku=sku, product=enriched_product, correlation_id=cid)
    await nexus_supervisor.record("shopware_sync","skill_shopware_sync", r2.ok, r2.latency_ms, correlation_id=cid)
    r3 = await nexus_registry.execute("skill_social_publish", product=enriched_product, sku=sku, correlation_id=cid)
    await nexus_supervisor.record("social_publish","skill_social_publish", r3.ok, r3.latency_ms, correlation_id=cid)
    await nexus_event("PipelineCompleted", {"sku": sku, "steps": ["ProductImported","skill_matterhorn_enrich","skill_shopware_sync","skill_social_publish"]}, source="nexus.pipeline", entity_type="product", entity_id=sku, correlation_id=cid)
    return {"ok": True, "correlation_id": cid, "sku": sku, "steps": {"enrich": r1.output, "shopware_sync": r2.output, "social": r3.output}, "events": await store.list_events(limit=20, correlation_id=cid)}

@router.post("/omnichannel/inbound")
async def nexus_omnichannel_inbound(payload: dict[str, Any] = Body(...)):
    _register_defaults(); await store.ensure_schema()
    channel = str(payload.get("channel") or "chat")
    out = await nexus_omnichannel.ingest(channel, payload)
    await nexus_supervisor.record("omnichannel","ingest", bool(out.get("ok")), 0)
    return out

@router.get("/social/queue")
async def nexus_social_queue(limit: int = 50):
    _register_defaults(); await store.ensure_schema()
    items = await store.list_events(limit=limit, event_type="SocialPublishQueued")
    return {"ok": True, "items": items}

@router.get("/inbox")
async def nexus_inbox(limit: int = 50):
    _register_defaults(); await store.ensure_schema()
    msgs = await store.list_events(limit=limit, event_type="CustomerMessageIn")
    calls = await store.list_events(limit=limit, event_type="CallEventIn")
    drafts = await store.list_events(limit=limit, event_type="CustomerDraftCreated")
    return {"ok": True, "messages": msgs, "calls": calls, "drafts": drafts}

NEXUS_HTML = r'''<!doctype html><html lang="de"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>NEXUS Multi-Agent Control</title>
<style>body{font-family:system-ui,Segoe UI,Arial;margin:0;background:#0b1020;color:#e8eefc}header{padding:16px 20px;background:#111735;border-bottom:1px solid #263056}h1{margin:0;font-size:20px}main{padding:16px;display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}.card{background:#121a3a;border:1px solid #263056;border-radius:10px;padding:12px}h2{margin:0 0 8px;font-size:15px;color:#9ec1ff}pre{white-space:pre-wrap;word-break:break-word;background:#0b1020;padding:8px;border-radius:8px;max-height:260px;overflow:auto;font-size:12px}.row{display:flex;gap:8px;flex-wrap:wrap}button{background:#2b5cff;color:#fff;border:0;padding:8px 10px;border-radius:8px;cursor:pointer}input,textarea{width:100%;background:#0b1020;color:#e8eefc;border:1px solid #263056;border-radius:8px;padding:8px}small{color:#9fb0d6}</style>
</head><body>
<header><h1>OMNI-CORE NEXUS – Multi-Agent Control</h1><small>Live Agents · Event-Stream · Skills/Knowledge · Omnichannel-Inbox · Social-Queue</small></header>
<main>
  <div class="card"><h2>Observer / Agentenstatus</h2><div class="row"><button onclick="loadStatus()">Aktualisieren</button><button onclick="loadEvents()">Events</button></div><pre id="status">lädt…</pre></div>
  <div class="card"><h2>Event-Stream (ESAA Nexus)</h2><pre id="events">-</pre></div>
  <div class="card"><h2>Skills & Knowledge</h2><pre id="skills">-</pre><small>Knowledge: knowledge/, config/knowledge/, docs/knowledge/ (RAG-light für matterhorn_enrich)</small></div>
  <div class="card"><h2>Omnichannel Inbox (WA/Chat/Call + KI-Entwürfe)</h2><button onclick="loadInbox()">Inbox laden</button><pre id="inbox">-</pre></div>
  <div class="card"><h2>Social Upload Queue</h2><button onclick="loadSocial()">Queue laden</button><pre id="social">-</pre></div>
  <div class="card"><h2>Simulation: eingehende WhatsApp-Bestellanfrage</h2>
    <textarea id="wa" rows="5">{"channel":"whatsapp","from":"+491701234567","text":"Hallo, wo ist meine Bestellung 100234?","order_id":"100234"}</textarea>
    <div class="row"><button onclick="sendInbound()">Senden (PII → omni-local-gdpr)</button></div><pre id="wa_out">-</pre></div>
</main>
<script>
async function j(u,o){const r=await fetch(u,Object.assign({headers:{'Content-Type':'application/json'}},o||{}));return r.json();}
async function loadStatus(){const d=await j('/api/nexus/status');status.textContent=JSON.stringify(d,null,2);skills.textContent=JSON.stringify(d.skills||[],null,2);} 
async function loadEvents(){events.textContent=JSON.stringify(await j('/api/nexus/events?limit=40'),null,2);} 
async function loadInbox(){inbox.textContent=JSON.stringify(await j('/api/nexus/inbox?limit=30'),null,2);} 
async function loadSocial(){social.textContent=JSON.stringify(await j('/api/nexus/social/queue?limit=30'),null,2);} 
async function sendInbound(){try{const p=JSON.parse(wa.value);wa_out.textContent=JSON.stringify(await j('/api/nexus/omnichannel/inbound',{method:'POST',body:JSON.stringify(p)}),null,2);await loadEvents();await loadInbox();}catch(e){wa_out.textContent=String(e);}}
loadStatus();loadEvents();
</script></body></html>'''

@router.get("/ui", response_class=HTMLResponse)
async def nexus_ui():
    _register_defaults()
    return HTMLResponse(NEXUS_HTML)
