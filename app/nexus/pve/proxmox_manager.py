"""Proxmox Manager AI assistant logic: chat, server plans, guided actions (OmniRoute-connected)."""
from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from app.nexus.event_bus.store import NexusEventStore, nexus_event

store = NexusEventStore()

QUICK_TASKS = [
    {"id": "status", "label": "Node-Status", "hint": "Aktuellen Cluster/Node-Status abrufen"},
    {"id": "tune", "label": "Node optimieren", "hint": "Sysctl/ksmtuned/fstrim Tuning anwenden", "action": {"type": "tune_node", "params": {}}},
    {"id": "fix", "label": "Probleme beheben", "hint": "Häufige PVE-Dienste prüfen/restarten", "action": {"type": "fix_common", "params": {}}},
    {"id": "backup", "label": "Backup CT", "hint": "vzdump für Container", "action": {"type": "backup_ct", "params": {"ctid": "201", "mode": "snapshot"}}},
    {"id": "report", "label": "Sofort-Report", "hint": "Agent-Heartbeat jetzt senden", "action": {"type": "report_now", "params": {}}},
    {"id": "install_omniroute", "label": "Install: OmniRoute", "hint": "Alpine LXC (2c/2G/8G) erstellen", "action": {"type": "create_lxc", "params": {"hostname": "omniroute-gateway", "cores": 2, "memory_mb": 2048, "disk_gb": 8, "ip": "dhcp"}}},
    {"id": "install_middleware", "label": "Install: Middleware", "hint": "OMNI-CORE LXC (4c/4G/16G) erstellen", "action": {"type": "create_lxc", "params": {"hostname": "omnicore-middleware", "cores": 4, "memory_mb": 4096, "disk_gb": 16, "ip": "dhcp"}}},
    {"id": "install_shopware", "label": "Install: Shopware", "hint": "Shopware 6 LXC (4c/4G/20G) erstellen", "action": {"type": "create_lxc", "params": {"hostname": "shopware-server", "cores": 4, "memory_mb": 4096, "disk_gb": 20, "ip": "dhcp"}}},
]


def _omniroute_base() -> str:
    base = os.getenv("OMNIROUTE_URL") or os.getenv("OMNI_ROUTE_BASE_URL") or os.getenv("OMNIROUTE_BASE_URL") or "http://192.168.176.22:20128"
    return base.rstrip("/")


def _fallback_plan(requirements: str, target: str = "lxc") -> dict[str, Any]:
    r = requirements.lower()
    base = {
        "hostname": "nexus-server",
        "type": target,
        "cores": 2,
        "memory_mb": 2048,
        "disk_gb": 8,
        "storage": "local",
        "bridge": "vmbr0",
        "ip": "dhcp",
        "os_template": "alpine-3.*-amd64",
        "arch": "amd64",
        "unprivileged": True,
        "services": [],
    }
    if "shopware" in r:
        base.update({"hostname": "shopware-server", "cores": 4, "memory_mb": 4096, "disk_gb": 20, "services": ["mariadb", "php-fpm83", "nginx", "shopware-worker", "shopware-scheduler"]})
    elif "omniroute" in r:
        base.update({"hostname": "omniroute-gateway", "cores": 2, "memory_mb": 2048, "disk_gb": 8, "services": ["omniroute"]})
    elif "omnicore" in r or "middleware" in r or "nexus" in r:
        base.update({"hostname": "omnicore-middleware", "cores": 4, "memory_mb": 4096, "disk_gb": 16, "services": ["omni-core", "uvicorn"]})
    cmd = f"pct create <CTID> local:vztmpl/{base['os_template']} --hostname {base['hostname']} --cores {base['cores']} --memory {base['memory_mb']} --storage local --rootfs local:{base['disk_gb']} --net0 name=eth0,bridge={base['bridge']},ip={base['ip']} --ostype alpine --unprivileged 1 --features nesting=1,keyctl=1 --onboot 1"
    return {
        "ok": True,
        "requirements": requirements,
        "target": target,
        "suggested": base,
        "ai_plan": None,
        "model": None,
        "via": "fallback",
        "commands": [cmd] if target == "lxc" else [],
        "notes": [
            "Storage: local, Arch: amd64, LXC: Alpine/OpenRC, unprivileged.",
            "Installationsreihenfolge: 1) OmniRoute, 2) Middleware, 3) Backup alter Container, 4) weitere Dienste (z.B. Shopware).",
        ],
    }


async def generate_server_plan(requirements: str, target: str = "lxc", hostname: str | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Generate AI-assisted server plan via OmniRoute/AI orchestrator with fallback."""
    requirements = (requirements or "").strip()
    target = target if target in ("lxc", "vm") else "lxc"
    context = context or {}
    model_used = None
    ai_text = None
    system = (
        "Du bist der OMNI-CORE Proxmox Manager KI-Assistent. Erzeuge einen präzisen, umsetzbaren Serverplan für Proxmox VE. "
        "Bevorzuge native Alpine Linux LXC (amd64, storage local, OpenRC, unprivileged) wenn passend. "
        "Antworte als JSON mit Feldern: title, summary, suggested{hostname,type,cores,memory_mb,disk_gb,storage,bridge,ip,os_template,arch,services[],ports[]}, "
        "network{bridge,vlan,ip,gw,dns}, install_steps[], fix_steps[], security_notes[], commands[]. Keine erfundenen Secrets."
    )
    user = (
        f"Anforderungen: {requirements}\nZieltyp: {target}\nHostname-Wunsch: {hostname or '-'}\n"
        f"Kontext (Nodes/Status): {json.dumps(context, ensure_ascii=False)[:4000]}\n"
        "Reihenfolge beachten: erst OmniRoute, dann Middleware, danach Backup alter Container, danach weitere Server."
    )
    try:
        from app.services.ai_orchestrator import get_ai_orchestrator  # lazy
        orch = get_ai_orchestrator()
        resp = await orch.chat(messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], task="copilot_admin")
        ai_text = resp.get("content") or resp.get("text") or (json.dumps(resp, ensure_ascii=False) if isinstance(resp, dict) else str(resp))
        model_used = resp.get("model") or resp.get("combo") or "ai_orchestrator"
    except Exception:
        ai_text = None
    plan = _fallback_plan(requirements, target)
    if hostname:
        plan["suggested"]["hostname"] = hostname
    plan["ai_plan"] = ai_text
    plan["model"] = model_used
    plan["via"] = "omniroute" if ai_text else "fallback"
    plan["omniroute_base"] = _omniroute_base()
    # try to extract JSON block from ai_text
    if ai_text:
        try:
            m = re.search(r"\{[\s\S]*\}", ai_text)
            if m:
                parsed = json.loads(m.group(0))
                if isinstance(parsed, dict):
                    plan["ai_structured"] = parsed
                    sug = parsed.get("suggested")
                    if isinstance(sug, dict):
                        plan["suggested"].update({k: v for k, v in sug.items() if v is not None})
        except Exception:
            pass
    await nexus_event("PveServerPlanGenerated", {"requirements": requirements[:500], "target": target, "hostname": plan["suggested"].get("hostname"), "via": plan["via"]}, source="proxmox-manager", entity_type="plan", entity_id=str(plan["suggested"].get("hostname") or "plan"))
    return plan


def _detect_intent(message: str) -> dict[str, Any]:
    m = (message or "").lower()
    intent = {"kind": "chat", "action": None, "params": {}, "quick_task": None}
    if any(k in m for k in ["firewall", "pve-firewall", "iptables", "regel", "port frei", "freigabe", "segmentation", "segmentierung"]):
        intent.update({"kind": "firewall_plan"})
    elif any(k in m for k in ["vlan", "netzwerk", "network", "bridge", "vmbr", "subnetz", "subnet", "routing", "trunk", "tagging"]):
        intent.update({"kind": "network_plan"})
    elif any(k in m for k in ["optim", "tune", "performance"]):
        intent.update({"kind": "action", "action": "tune_node", "params": {}, "quick_task": "tune"})
    elif any(k in m for k in ["fix", "repar", "problem", "fehler", "kaputt", "dienst"]):
        intent.update({"kind": "action", "action": "fix_common", "params": {}, "quick_task": "fix"})
    elif "backup" in m or "sicher" in m:
        ctid = None
        mm = re.search(r"\b(\d{2,6})\b", m)
        if mm:
            ctid = mm.group(1)
        intent.update({"kind": "action", "action": "backup_ct", "params": {"ctid": ctid or "201", "mode": "snapshot"}, "quick_task": "backup"})
    elif "status" in m or "monitor" in m:
        intent.update({"kind": "status", "quick_task": "status"})
    elif any(k in m for k in ["plan", "server", "install", "shopware", "omniroute", "omnicore", "middleware", "lxc", "vm"]):
        intent.update({"kind": "plan"})
    return intent


async def manager_chat(message: str, node: str | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Chat endpoint for Proxmox Manager: answers, proposes plans/actions via OmniRoute."""
    await store.ensure_schema()
    message = (message or "").strip()
    context = context or {}
    node = node or context.get("node") or "*"
    intent = _detect_intent(message)
    result: dict[str, Any] = {"ok": True, "intent": intent, "node": node, "omniroute_base": _omniroute_base()}

    if intent["kind"] == "network_plan":
        plan = await generate_network_plan(message, node=node, context=context)
        result.update({"type": "network_plan", "reply": "Netzwerk-/VLAN-Konzept erstellt (OmniRoute-gestützt)." if plan.get("via") == "omniroute" else "Netzwerk-Konzept (Fallback) erstellt.", "plan": plan})
        return result

    if intent["kind"] == "firewall_plan":
        plan = await generate_firewall_plan(message, node=node, scope=context.get("scope") or "cluster", context=context)
        result.update({"type": "firewall_plan", "reply": "Firewall-Plan erstellt (OmniRoute-gestützt)." if plan.get("via") == "omniroute" else "Firewall-Plan (Fallback) erstellt.", "plan": plan})
        return result

    if intent["kind"] == "plan" or (intent["kind"] == "chat" and len(message) > 20):
        plan = await generate_server_plan(message, target=context.get("target") or "lxc", hostname=context.get("hostname"), context=context)
        result.update({"type": "plan", "reply": "Serverplan erstellt (OmniRoute-gestützt)." if plan.get("via") == "omniroute" else "Serverplan (Fallback) erstellt.", "plan": plan})
        return result

    if intent["kind"] in ("action", "status"):
        action = {"type": intent.get("action") or "report_now", "params": intent.get("params") or {}}
        # queue action if actionable
        if intent.get("action"):
            try:
                from app.nexus.pve.routes import _ACTIONS  # shared in-process queue
                act = {"id": str(uuid.uuid4()), "node": node, "type": action["type"], "params": action["params"], "requested_by": "proxmox-manager", "created_at": __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(), "status": "queued"}
                _ACTIONS.setdefault(node, []).append(act)
                await nexus_event("PveActionQueued", act, source="proxmox-manager", entity_type="pve_node", entity_id=node)
                result.update({"type": "action", "reply": f"Aktion `{action['type']}` für Node `{node}` in Queue gestellt.", "action": act})
            except Exception as e:
                result.update({"type": "action", "reply": f"Aktion erkannt ({action['type']}), aber Queue fehlgeschlagen: {e}", "action": action, "error": str(e)})
        else:
            result.update({"type": "status", "reply": "Node-Status abrufen: /api/nexus/pve/nodes oder Schnellmenü 'Node-Status'."})
        return result

    # general chat via AI
    reply = "Nutze das Schnellmenü (Tune/Fix/Backup/Install) oder frage nach einem Serverplan (z.B. 'Plane Shopware LXC 4c/4GB auf local')."
    try:
        from app.services.ai_orchestrator import get_ai_orchestrator
        orch = get_ai_orchestrator()
        sysmsg = "Du bist der Proxmox Manager Assistent (OMNI-CORE NEXUS). Hilf bei Serverplanung, schneller Installation (OmniRoute/Middleware/Shopware), Tuning, Backups und Problemlösung auf Proxmox VE (Alpine LXC amd64, storage local, OpenRC). Gib kurze, konkrete Schritte."
        resp = await orch.chat(messages=[{"role": "system", "content": sysmsg}, {"role": "user", "content": message}], task="copilot_admin")
        reply = resp.get("content") or resp.get("text") or reply
        result["model"] = resp.get("model") or resp.get("combo")
        result["via"] = "omniroute"
    except Exception:
        result["via"] = "fallback"
    result.update({"type": "chat", "reply": reply})
    await nexus_event("ProxmoxManagerChat", {"node": node, "intent": intent.get("kind"), "via": result.get("via")}, source="proxmox-manager", entity_type="chat", entity_id=node or "*")
    return result

# ---- Network & Firewall planning / automation (AI-assisted, OmniRoute-connected) ----

async def generate_network_plan(requirements: str, node: str = "*", context: dict[str, Any] | None = None) -> dict[str, Any]:
    await store.ensure_schema()
    requirements = (requirements or "").strip()
    context = context or {}
    system = (
        "Du bist Netzwerk- und Proxmox-VE-Architekt. Erstelle ein konkretes, sicheres Netzwerk-Konzept für Proxmox (Bridges, VLANs, Subnetze, Routing, Firewall-Zonen). "
        "Antworte als JSON mit Feldern: title, summary, assumptions[], vlans[{id,name,subnet,gateway,purpose,bridge,tagged_on[]}], bridges[{name,ports,vlan_aware,comment}], "
        "ip_plan[{host_or_service,vlan,ip,cidr,gw}], interfaces_snippet_debian_ifupdown, pve_commands[], firewall_policy{default_in,default_out,rules[]}, validation_steps[], risks[]. "
        "Bevorzuge Linux bridges (vmbrX) vlan-aware; keine erfundenen Secrets."
    )
    user = f"Node/Ziel: {node}\nAnforderungen: {requirements}\nKontext: {json.dumps(context, ensure_ascii=False)[:3000]}"
    ai_text, model_used = None, None
    try:
        from app.services.ai_orchestrator import get_ai_orchestrator
        orch = get_ai_orchestrator()
        resp = await orch.chat_completion(messages=[{"role":"system","content":system},{"role":"user","content":user}], module="nexus.network.plan", combo="omni-admin-agent", model="omni-admin-agent", task="copilot_admin")
        ai_text = resp.get("content") or ""
        model_used = resp.get("model")
    except Exception:
        ai_text = None
    plan = {"ok": True, "type": "network_plan", "node": node, "requirements": requirements, "ai_plan": ai_text, "model": model_used, "via": "omniroute" if ai_text else "fallback", "omniroute_base": _omniroute_base()}
    if ai_text:
        try:
            m = re.search(r"\{[\s\S]*\}", ai_text)
            if m: plan["structured"] = json.loads(m.group(0))
        except Exception: pass
    await nexus_event("PveNetworkPlanGenerated", {"node": node, "via": plan["via"]}, source="proxmox-manager", entity_type="network_plan", entity_id=node)
    return plan

async def generate_firewall_plan(requirements: str, node: str = "*", scope: str = "cluster", context: dict[str, Any] | None = None) -> dict[str, Any]:
    await store.ensure_schema()
    requirements = (requirements or "").strip()
    context = context or {}
    system = (
        "Du bist Proxmox-Firewall/Security-Experte. Erstelle ein konkretes pve-firewall Regelwerk (Cluster/Node/VM) mit VLAN/Netz-Segmentierung. "
        "Antworte als JSON mit Feldern: title, summary, scope(cluster|node|vm), enable_firewall{cluster,node,vm}, default_policies{input,output,forward}, "
        "ipsets[{name,comment,cidrs[]}], security_groups[{name,rules[]}], rules[{scope,action,type,source,dest,proto,dport,sport,iface,comment,enable}], "
        "pvesh_commands[], validation_steps[], risks[]. Nutze least-privilege; keine erfundenen Secrets."
    )
    user = f"Scope: {scope}\nNode/Ziel: {node}\nAnforderungen: {requirements}\nKontext: {json.dumps(context, ensure_ascii=False)[:3000]}"
    ai_text, model_used = None, None
    try:
        from app.services.ai_orchestrator import get_ai_orchestrator
        orch = get_ai_orchestrator()
        resp = await orch.chat_completion(messages=[{"role":"system","content":system},{"role":"user","content":user}], module="nexus.firewall.plan", combo="omni-admin-agent", model="omni-admin-agent", task="copilot_admin")
        ai_text = resp.get("content") or ""
        model_used = resp.get("model")
    except Exception:
        ai_text = None
    plan = {"ok": True, "type": "firewall_plan", "node": node, "scope": scope, "requirements": requirements, "ai_plan": ai_text, "model": model_used, "via": "omniroute" if ai_text else "fallback", "omniroute_base": _omniroute_base()}
    if ai_text:
        try:
            m = re.search(r"\{[\s\S]*\}", ai_text)
            if m: plan["structured"] = json.loads(m.group(0))
        except Exception: pass
    await nexus_event("PveFirewallPlanGenerated", {"node": node, "scope": scope, "via": plan["via"]}, source="proxmox-manager", entity_type="firewall_plan", entity_id=node)
    return plan
