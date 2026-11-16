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
from app.nexus.pve.routes import router as pve_router
from app.nexus.orchestration.routes import router as orch_router
from database import get_connection, uses_postgres, get_pg_pool

router = APIRouter(prefix="/api/nexus", tags=["nexus"])
router.include_router(pve_router)
router.include_router(orch_router)
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
  <div class="card"><h2>PVE Nodes (Agent Monitoring)</h2><div class="row"><button onclick="loadPve()">Aktualisieren</button></div><pre id="pve_nodes">-</pre><small>Agent meldet CPU/RAM/Disk/Guests per Token an /api/nexus/pve/report</small></div>
  <div class="card"><h2>PVE KI-Assistent & Schnellmenü</h2>
    <div class="row">
      <button onclick="quickTask('tune')">Node optimieren</button>
      <button onclick="quickTask('fix')">Probleme beheben</button>
      <button onclick="quickTask('backup')">Backup (CT)</button>
      <button onclick="quickTask('report')">Status melden</button>
    </div>
    <div class="row">
      <button onclick="quickTask('create_omniroute')">Install: OmniRoute</button>
      <button onclick="quickTask('create_omnicore')">Install: Middleware</button>
      <button onclick="quickTask('create_shopware')">Install: Shopware</button>
    </div>
    <div id="pve_chat" style="background:#0b1020;border:1px solid #263056;border-radius:8px;padding:8px;height:150px;overflow:auto;font-size:12px;margin:8px 0"><div><b>KI:</b> Hallo! Ich helfe bei Serverplänen, schneller Installation, Tuning und Fixes auf den PVE-Nodes. Node im Feld unten wählen, dann Schnellmenü nutzen oder Anfrage tippen.</div></div>
    <div class="row"><input id="pve_chat_in" placeholder="z.B. Plane Shopware Server 4c/4GB, optimiere Node pve01, fixe Probleme…" onkeydown="if(event.key==='Enter'){chatSend();}"/><button onclick="chatSend()">Senden</button></div>
  </div>
  <div class="card"><h2>AI Serverplan & Direkt-Entwicklung auf Nodes</h2>
    <input id="pve_host" placeholder="Hostname (optional)" />
    <select id="pve_target"><option value="lxc">LXC (Alpine/native)</option><option value="vm">VM</option></select>
    <textarea id="pve_req" rows="4" placeholder="Anforderungen, z.B. Shopware 6 Server mit MariaDB/PHP-FPM/Nginx, 4c/4GB, Performance optimiert, Middleware-Anbindung vorkonfiguriert"></textarea>
    <div class="row"><button onclick="genPlan()">KI-Serverplan generieren</button></div><pre id="pve_plan">-</pre></div>
  <div class="card"><h2>PVE Aktionen (install/fix/tune/backup)</h2>
    <input id="pve_node" placeholder="Node-Name (hostname -s) oder *" />
    <select id="pve_action"><option value="tune_node">tune_node</option><option value="fix_common">fix_common</option><option value="backup_ct">backup_ct</option><option value="create_lxc">create_lxc</option><option value="pct_exec">pct_exec</option><option value="report_now">report_now</option></select>
    <textarea id="pve_params" rows="4" placeholder='Params JSON, z.B. {"ctid":"201"} oder {"hostname":"shopware","cores":4,"memory_mb":4096,"disk_gb":20,"ip":"dhcp"}'></textarea>
    <div class="row"><button onclick="queuePveAction()">Aktion an Agent senden</button></div>
    <pre id="pve_action_out">-</pre><h2>Letzte Aktionsergebnisse</h2><pre id="pve_results">-</pre></div>
</main>
<script>
async function j(u,o){const r=await fetch(u,Object.assign({headers:{'Content-Type':'application/json'}},o||{}));return r.json();}
async function loadStatus(){const d=await j('/api/nexus/status');status.textContent=JSON.stringify(d,null,2);skills.textContent=JSON.stringify(d.skills||[],null,2);} 
async function loadEvents(){events.textContent=JSON.stringify(await j('/api/nexus/events?limit=40'),null,2);} 
async function loadInbox(){inbox.textContent=JSON.stringify(await j('/api/nexus/inbox?limit=30'),null,2);} 
async function loadSocial(){social.textContent=JSON.stringify(await j('/api/nexus/social/queue?limit=30'),null,2);} 
async function loadPve(){try{pve_nodes.textContent=JSON.stringify(await j('/api/nexus/pve/nodes?limit=100'),null,2);pve_results.textContent=JSON.stringify(await j('/api/nexus/pve/actions/results?limit=20'),null,2);}catch(e){pve_nodes.textContent=String(e);} }
async function genPlan(){try{pve_plan.textContent='…';pve_plan.textContent=JSON.stringify(await j('/api/nexus/pve/plan',{method:'POST',body:JSON.stringify({requirements:pve_req.value,target:pve_target.value,hostname:pve_host.value})}),null,2);}catch(e){pve_plan.textContent=String(e);}}
async function queuePveAction(){try{const params=JSON.parse(pve_params.value||'{}');pve_action_out.textContent=JSON.stringify(await j('/api/nexus/pve/actions',{method:'POST',body:JSON.stringify({node:pve_node.value,type:pve_action.value,params})}),null,2);await loadPve();}catch(e){pve_action_out.textContent=String(e);}}
function quickTask(t){const n=pve_node.value||'*'; if(t==='tune'){pve_action.value='tune_node';pve_params.value='{}';}
  if(t==='fix'){pve_action.value='fix_common';pve_params.value='{}';}
  if(t==='report'){pve_action.value='report_now';pve_params.value='{}';}
  if(t==='backup'){pve_action.value='backup_ct';pve_params.value='{"ctid":"201","mode":"snapshot"}';}
  if(t==='create_omniroute'){pve_action.value='create_lxc';pve_params.value='{"hostname":"omniroute-gateway","cores":2,"memory_mb":2048,"disk_gb":8,"ip":"dhcp"}';}
  if(t==='create_omnicore'){pve_action.value='create_lxc';pve_params.value='{"hostname":"omnicore-middleware","cores":4,"memory_mb":4096,"disk_gb":16,"ip":"dhcp"}';}
  if(t==='create_shopware'){pve_action.value='create_lxc';pve_params.value='{"hostname":"shopware-server","cores":4,"memory_mb":4096,"disk_gb":20,"ip":"dhcp"}';}
  pve_node.value=n; chatLog('System','Quick-Task gewählt: '+t+' (Node '+n+'). Prüfe Params und klicke "Aktion an Agent senden".');}
function chatLog(w,m){const el=document.getElementById('pve_chat'); const d=document.createElement('div'); d.innerHTML='<b>'+w+':</b> '+m; el.appendChild(d); el.scrollTop=el.scrollHeight;}
async function chatSend(){const msg=(pve_chat_in.value||'').trim(); if(!msg) return; chatLog('Du',msg); pve_chat_in.value=''; const m=msg.toLowerCase();
  if(m.includes('plan')||m.includes('server')||m.includes('shopware')||m.includes('omniroute')||m.includes('omnicore')||m.includes('middleware')){ pve_req.value=msg; await genPlan(); chatLog('KI','Serverplan erstellt (siehe Panel).'); return; }
  if(m.includes('tune')||m.includes('optim')){ quickTask('tune'); await queuePveAction(); chatLog('KI','Tune-Aktion gesendet.'); return; }
  if(m.includes('fix')||m.includes('repar')||m.includes('problem')){ quickTask('fix'); await queuePveAction(); chatLog('KI','Fix-Aktion gesendet.'); return; }
  if(m.includes('backup')||m.includes('sicher')){ quickTask('backup'); chatLog('KI','Backup-Vorlage gesetzt – CTID prüfen und senden.'); return; }
  if(m.includes('status')||m.includes('node')){ await loadPve(); chatLog('KI','Node-Status aktualisiert.'); return; }
  chatLog('KI','Nutze das Schnellmenü (Tune/Fix/Backup/Install) oder schreibe z.B. "Plane Shopware Server 4c/4GB" oder "Optimiere Node pve01".');}
async function sendInbound(){try{const p=JSON.parse(wa.value);wa_out.textContent=JSON.stringify(await j('/api/nexus/omnichannel/inbound',{method:'POST',body:JSON.stringify(p)}),null,2);await loadEvents();await loadInbox();}catch(e){wa_out.textContent=String(e);}}
loadStatus();loadEvents();loadPve();
</script></body></html>'''

@router.get("/ui", response_class=HTMLResponse)
async def nexus_ui():
    _register_defaults()
    return HTMLResponse(NEXUS_HTML)
