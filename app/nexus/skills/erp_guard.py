"""skill_erp_guard: payment/order status checks before customer responses/actions."""
from __future__ import annotations
from typing import Any
from database import fetch_order
from app.nexus.event_bus.store import nexus_event

PAID_STATES = {"paid", "authorized", "captured", "completed", "settled"}
BLOCKED_FULFILLMENT = {"blocked", "hold", "review", "fraud"}

async def skill_erp_guard(order_id: str = "", phone: str = "", email: str = "", action: str = "respond", **_: Any) -> dict[str, Any]:
    if not order_id:
        await nexus_event("ErpGuardChecked", {"order_id": order_id, "allowed": False, "reason": "missing_order_id"}, source="skill_erp_guard", entity_type="order")
        return {"ok": False, "allowed": False, "reason": "missing_order_id", "requires_manual_review": True}
    order = await fetch_order(str(order_id))
    if not order:
        res = {"ok": True, "allowed": False, "reason": "order_not_found", "order_id": order_id}
        await nexus_event("ErpGuardChecked", {**res, "action": action}, source="skill_erp_guard", entity_type="order", entity_id=str(order_id))
        return res
    pay = str(order.get("payment_status") or order.get("paymentState") or "").lower()
    fulfill = str(order.get("fulfillment_status") or order.get("status") or "").lower()
    paid = pay in PAID_STATES
    blocked = fulfill in BLOCKED_FULFILLMENT or str(order.get("fulfillment_locked") or "").lower() in {"1","true","yes"}
    allowed = paid and not blocked
    reason = "ok" if allowed else ("payment_not_confirmed" if not paid else "fulfillment_blocked")
    res = {
        "ok": True,
        "allowed": allowed,
        "reason": reason,
        "order_id": order_id,
        "payment_status": pay,
        "fulfillment_status": fulfill,
        "tracking_number": order.get("tracking_number") or order.get("tracking_code"),
        "safe_customer_reply_allowed": allowed or action in {"status_inquiry","shipping_info"},
    }
    await nexus_event("ErpGuardChecked", {**res, "action": action}, source="skill_erp_guard", entity_type="order", entity_id=str(order_id))
    return res
