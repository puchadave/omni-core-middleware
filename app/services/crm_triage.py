"""Omnichannel CRM smart triage & draft generation.

- Intent/sentiment classification and context-aware reply draft via AIOrchestrator.
- PII/customer data routed to omni-local-gdpr (enforced by orchestrator).
- Strict payment gate: never promise shipping/dispatch for unpaid/locked orders.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from database import crm_list_messages, fetch_order, get_setting, write_log
from app.services.ai_orchestrator import COMBO_CRM, COMBO_LOCAL_GDPR, get_ai_orchestrator

logger = logging.getLogger(__name__)

_PAYMENT_OK = {"paid", "succeeded", "captured", "settled", "complete", "completed"}


def _payment_ok(order: dict[str, Any] | None) -> bool:
    if not order:
        return False
    ps = str(order.get("payment_status") or "").strip().lower()
    locked = bool(order.get("fulfillment_locked"))
    return (ps in _PAYMENT_OK) and not locked


async def build_order_context(order_id: str | None) -> dict[str, Any] | None:
    if not order_id:
        return None
    try:
        o = await fetch_order(str(order_id))
        if not o:
            return None
        return {
            "order_id": o.get("order_id") or o.get("id"),
            "payment_status": o.get("payment_status"),
            "fulfillment_locked": bool(o.get("fulfillment_locked")),
            "status": o.get("status"),
            "tracking_number": o.get("tracking_number") or o.get("tracking") or "",
            "shipping_status": o.get("shipping_status") or "",
            "can_promise_shipping": _payment_ok(o),
        }
    except Exception:
        return None


async def triage_and_draft(*, channel: str, message_text: str, subject: str = "", customer_ref: str = "", order_id: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
    orch = get_ai_orchestrator()
    order_ctx = await build_order_context(order_id)
    history = []
    if thread_id:
        try:
            msgs = await crm_list_messages(thread_id=str(thread_id), limit=6)
            for m in msgs[-6:]:
                history.append({"direction": m.get("direction"), "body": (m.get("body") or "")[:800]})
        except Exception:
            pass
    can_ship = bool(order_ctx and order_ctx.get("can_promise_shipping"))
    gate_note = (
        "ZAHLUNGS-GATE: Bestellung ist bezahlt und nicht gesperrt; Versandstatus darf sachlich genannt werden."
        if can_ship else
        "ZAHLUNGS-GATE: KEINE Versand-/Lieferzusage machen (Zahlung nicht bestätigt oder fulfillment_locked oder unbekannt). Höflich um Klärung/Zahlungsprüfung bitten."
    )
    prompt = (
        "Analysiere die Kundenanfrage und liefere ein JSON-Objekt mit Feldern: "
        '{"intent": one of ["lieferstatus","retoure","zahlungsfrage","produktberatung","storno","beschwerde","sonstiges"], '
        '"sentiment": one of ["positiv","neutral","negativ","dringend"], "urgency": one of ["low","medium","high"], '
        '"summary": str, "draft_reply_de": str, "next_actions": [str], "payment_gate_respected": true/false}. '
        f"{gate_note} Nutze nur bekannte Fakten (Bestellkontext). Keine erfundenen Trackingdaten. "
        f"Kanal: {channel}. Betreff: {subject}. Kunde/Ref: {customer_ref}. "
        f"Bestellkontext: {json.dumps(order_ctx, ensure_ascii=False)}. "
        f"Historie: {json.dumps(history, ensure_ascii=False)}. "
        f"Nachricht: {message_text[:6000]}"
    )
    # contains customer/order content -> orchestrator will force omni-local-gdpr when PII detected; also prefer local for CRM by context
    force_local = True if (order_ctx or customer_ref or "@" in (message_text or "")) else False
    res = await orch.chat_completion(
        messages=[{"role": "system", "content": "Du bist CRM-Triage- und Antwortassistent für E-Commerce. Antworte ausschließlich mit JSON. Respektiere Zahlungs-/Versand-Gates strikt."},
                  {"role": "user", "content": prompt}],
        module="crm_triage",
        combo=COMBO_LOCAL_GDPR if force_local else COMBO_CRM,
        task="crm_triage_draft",
        use_cache=False,
        temperature=0.2,
        force_local=force_local,
    )
    parsed: dict[str, Any] = {}
    if res.get("ok"):
        content = res.get("content") or ""
        try:
            s = content.strip()
            if "```" in s:
                s = s.split("```", 2)[1]
                if s.startswith("json"):
                    s = s[4:]
            i, j = s.find("{"), s.rfind("}")
            if i != -1 and j != -1:
                parsed = json.loads(s[i:j+1])
        except Exception:
            parsed = {"summary": (content[:500] if content else ""), "draft_reply_de": content or ""}
    draft = str(parsed.get("draft_reply_de") or parsed.get("draft") or "")
    # enforce gate server-side: strip/replace shipping promises when not allowed
    if not can_ship and draft:
        lower = draft.lower()
        if any(w in lower for w in ["wird versendet", "ist unterwegs", "versand erfolgt", "morgen zugestellt", "bereits verschickt", "shipped"]):
            parsed["draft_reply_de"] = (
                "Vielen Dank für Ihre Nachricht. Zum aktuellen Stand können wir noch keine Versandzusage machen, "
                "da die Zahlung/Bestellfreigabe noch nicht final bestätigt ist. "
                "Sobald die Zahlung eingegangen bzw. geprüft ist, informieren wir Sie umgehend über den Versand."
            )
            parsed["payment_gate_respected"] = True
            parsed["gate_enforced_server_side"] = True
    await write_log("INFO", "crm.triage_drafted", "CRM triage draft generated", {"channel": channel, "privacy_routed": res.get("privacy_routed"), "order_id": order_id})
    return {
        "ok": bool(res.get("ok")),
        "triage": {
            "intent": parsed.get("intent", "sonstiges"),
            "sentiment": parsed.get("sentiment", "neutral"),
            "urgency": parsed.get("urgency", "medium"),
            "summary": parsed.get("summary", ""),
        },
        "draft_reply_de": parsed.get("draft_reply_de") or parsed.get("draft") or "",
        "next_actions": parsed.get("next_actions") or [],
        "order_context": order_ctx,
        "payment_gate_respected": bool(parsed.get("payment_gate_respected", can_ship is False if order_ctx else True)),
        "privacy_routed": bool(res.get("privacy_routed")),
        "combo_used": res.get("model") or res.get("requested_model"),
        "error": res.get("error"),
    }
