"""skill_shopware_sync: bidirectional sync hooks for Shopware 6 (products/prices/stock/order status)."""
from __future__ import annotations
import os, logging
from typing import Any
import httpx
from app.nexus.event_bus.store import nexus_event

logger = logging.getLogger(__name__)

def _cfg() -> dict[str, str]:
    return {
        "base_url": (os.getenv("SHOPWARE_API_URL") or os.getenv("SHOPWARE_BASE_URL") or "").rstrip("/"),
        "client_id": os.getenv("SHOPWARE_CLIENT_ID") or os.getenv("SHOPWARE_API_KEY") or "",
        "client_secret": os.getenv("SHOPWARE_CLIENT_SECRET") or os.getenv("SHOPWARE_API_SECRET") or "",
        "access_token": os.getenv("SHOPWARE_ACCESS_TOKEN") or "",
    }

async def _token(cfg: dict[str, str]) -> str | None:
    if cfg.get("access_token"): return cfg["access_token"]
    if not (cfg["base_url"] and cfg["client_id"] and cfg["client_secret"]): return None
    url = f"{cfg['base_url']}/api/oauth/token"
    payload = {"grant_type": "client_credentials", "client_id": cfg["client_id"], "client_secret": cfg["client_secret"]}
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(url, json=payload)
        if r.status_code >= 300: return None
        return r.json().get("access_token")

async def skill_shopware_sync(action: str = "upsert_product", sku: str = "", product: dict[str, Any] | None = None, order_id: str = "", correlation_id: str = "", **_: Any) -> dict[str, Any]:
    cfg = _cfg()
    product = product or {}
    sku = sku or str(product.get("sku") or product.get("productNumber") or "")
    if not cfg["base_url"]:
        res = {"ok": False, "status": "not_configured", "action": action, "sku": sku, "reason": "missing_shopware_credentials_or_url", "hint": "Set SHOPWARE_API_URL/BASE_URL and SHOPWARE_CLIENT_ID/SECRET or SHOPWARE_ACCESS_TOKEN"}
        await nexus_event("ShopwareSyncDeferred", res, source="skill_shopware_sync", entity_type="product" if sku else "order", entity_id=sku or order_id, correlation_id=correlation_id)
        return res
    tok = await _token(cfg)
    if not tok:
        res = {"ok": False, "status": "auth_failed", "action": action, "sku": sku}
        await nexus_event("ShopwareSyncFailed", res, source="skill_shopware_sync", entity_type="product" if sku else "order", entity_id=sku or order_id, correlation_id=correlation_id)
        return res
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            if action == "get_order_status" and order_id:
                # best-effort: search order by orderNumber
                r = await c.post(f"{cfg['base_url']}/api/search/order", headers=headers, json={"filter":[{"type":"equals","field":"orderNumber","value":str(order_id)}], "limit":1})
                data = r.json() if r.headers.get("content-type","" ).startswith("application/json") else {}
                item = (data.get("data") or [None])[0]
                res = {"ok": r.status_code < 300 and bool(item), "action": action, "order_id": order_id, "order": item, "http_status": r.status_code}
            elif action in ("upsert_product","sync_product","sync_price_stock"):
                body = {
                    "productNumber": sku,
                    "name": product.get("title") or product.get("name") or sku,
                    "stock": int(product.get("stock") or product.get("stock_quantity") or 0),
                    "price": [{"currencyId": product.get("currencyId") or "b7d2554b0ce847cd82f3ac9bd1c0dfca", "gross": float(product.get("sale_price") or product.get("price") or 0), "net": float(product.get("sale_price") or product.get("price") or 0), "linked": True}],
                    "taxId": product.get("taxId") or "c081d7cdd5b14b0fa4e2d8ab7d1cb2e4",
                    "active": True,
                }
                # Shopware upsert by productNumber is non-trivial without id; attempt sync endpoint pattern fallback
                r = await c.post(f"{cfg['base_url']}/api/_action/sync", headers=headers, json={"entity":"product","action":"upsert","payload":[{k:v for k,v in body.items() if v not in (None,"")}]})
                res = {"ok": r.status_code < 300, "action": action, "sku": sku, "http_status": r.status_code, "response": (r.text[:500] if r.text else "")}
            else:
                res = {"ok": False, "action": action, "sku": sku, "reason": "unsupported_action"}
        await nexus_event("ShopwareSynced" if res.get("ok") else "ShopwareSyncFailed", res, source="skill_shopware_sync", entity_type="product" if sku else "order", entity_id=sku or order_id, correlation_id=correlation_id)
        return res
    except Exception as e:
        logger.exception("shopware sync error")
        res = {"ok": False, "action": action, "sku": sku, "error": f"{type(e).__name__}:{e}"}
        await nexus_event("ShopwareSyncFailed", res, source="skill_shopware_sync", entity_type="product" if sku else "order", entity_id=sku or order_id, correlation_id=correlation_id)
        return res
