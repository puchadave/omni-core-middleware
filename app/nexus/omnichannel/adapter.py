"""Unified omnichannel adapter: WhatsApp (wacli), webchat, call events, Shopware inquiries.
DSGVO firewall: messages containing PII are forced via AIOrchestrator to omni-local-gdpr.
"""
from __future__ import annotations
import re, json, logging
from typing import Any
from app.services.ai_orchestrator import get_ai_orchestrator, detect_pii, COMBO_CRM, COMBO_LOCAL_GDPR
from app.nexus.event_bus.store import nexus_event
from app.nexus.skills.erp_guard import skill_erp_guard

logger = logging.getLogger(__name__)
ORDER_RE = re.compile(r"\b([A-Z0-9]{5,}|#?\d{4,})\b")

def _extract_order_id(text: str) -> str | None:
    # prefer explicit order labels
    m = re.search(r"(bestell(?:ung|nummer)?|order|auftrag)[^\w#]*([A-Z0-9#-]{4,})", text or "", re.I)
    if m: return m.group(2).lstrip('#')
    # fallback: standalone token not phone-like
    for t in ORDER_RE.findall(text or ""):
        if len(t) >= 5 and not re.fullmatch(r"\+?\d{7,}", t): return t.lstrip('#')
    return None

class NexusOmnichannelAdapter:
    async def ingest(self, channel: str, payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("text") or payload.get("message") or payload.get("body") or "")
        sender = str(payload.get("from") or payload.get("phone") or payload.get("email") or payload.get("customer") or "")
        call_meta = payload.get("call") or payload.get("call_event") or {}
        order_id = str(payload.get("order_id") or _extract_order_id(text) or "")
        pii = detect_pii(" ".join([text, sender, json.dumps(call_meta, ensure_ascii=False, default=str), order_id]))
        base_event = {"channel": channel, "sender_present": bool(sender), "has_order_id": bool(order_id), "pii": pii}
        ev_type = "CallEventIn" if (channel in ("voice","call") or call_meta) else "CustomerMessageIn"
        rec = await nexus_event(ev_type, base_event, source="nexus.omnichannel", entity_type="customer", entity_id=order_id or sender[:64], correlation_id=str(payload.get("correlation_id") or ""))

        guard = None
        if order_id:
            guard = await skill_erp_guard(order_id=order_id, action="respond")
        orch = get_ai_orchestrator()
        # enforce local-only when PII present (orchestrator also enforces; we state intent explicitly)
        combo = COMBO_LOCAL_GDPR if pii.get("pii") else COMBO_CRM
        sys = (
            "Du bist der OMNI-CORE Kundenservice-Agent. Nutze ausschließlich bereitgestellten Kontext. "
            "Prüfe Zahlungs-/Versandstatus vor Zusagen. Bei fehlender Zahlungsfreigabe oder fehlenden Daten: "
            "keine Zusage, erstelle einen internen Antwortentwurf und markiere manuelle Prüfung. "
            "Standardfragen (Sendungsverfolgung) nur beantworten, wenn Status/Tracking sicher vorliegen. "
            "Antworte als JSON: {draft_reply, auto_send_allowed, reason, required_checks[], intent, language}."
        )
        ctx = {"channel": channel, "sender": sender, "order_id": order_id, "erp_guard": guard, "payload_excerpt": {k: payload.get(k) for k in ("subject","thread_id","shopware_customer_id") if k in payload}}
        res = await orch.chat_completion(
            messages=[{"role":"system","content":sys}, {"role":"user","content":f"Kontext:{json.dumps(ctx, ensure_ascii=False)[:6000]}\nNachricht:{text[:4000]}"}],
            module="nexus_omnichannel", combo=combo, task="customer_reply_draft", use_cache=False, temperature=0.1,
            context_text=" ".join([text, sender, order_id, json.dumps(call_meta, default=str)]),
        )
        content = res.get("content") or ""
        draft = None
        try:
            s=content; i,j=s.find("{"),s.rfind("}")
            if i!=-1 and j>i: draft=json.loads(s[i:j+1])
        except Exception:
            draft = {"draft_reply": content, "auto_send_allowed": False, "reason": "unparsed"}
        # hard guard: never auto-send if payment not confirmed/blocked and action implies commitment
        if guard and not guard.get("allowed"):
            if isinstance(draft, dict):
                draft["auto_send_allowed"] = False
                draft.setdefault("required_checks", []).append("erp_guard:"+str(guard.get("reason")))
        out = {
            "ok": bool(res.get("ok", True)),
            "channel": channel,
            "event": rec,
            "pii_routed_local": bool(pii.get("pii") or res.get("privacy_routed")),
            "combo_used": res.get("model") or combo,
            "order_id": order_id or None,
            "erp_guard": guard,
            "draft": draft,
        }
        await nexus_event("CustomerDraftCreated", {"channel": channel, "order_id": order_id, "auto": bool((draft or {}).get("auto_send_allowed")), "pii_local": out["pii_routed_local"]}, source="nexus.omnichannel", entity_type="customer", entity_id=order_id or sender[:64], correlation_id=str(payload.get("correlation_id") or ""))
        return out

nexus_omnichannel = NexusOmnichannelAdapter()
