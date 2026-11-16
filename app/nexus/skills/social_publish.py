"""Automated Social-Commerce pipeline: caption/hashtag gen + publish planning via social-auto-upload adapters."""
from __future__ import annotations
import json
from typing import Any
from app.services.ai_orchestrator import get_ai_orchestrator
from services.social_auto_upload import plan_publish_job, supported_platforms
from app.nexus.event_bus.store import nexus_event

async def skill_social_publish(product: dict[str, Any], *, sku: str = "", platforms: list[str] | None = None, account: str = "default", media_url: str = "", correlation_id: str = "", **_: Any) -> dict[str, Any]:
    sku = sku or str(product.get("sku") or "")
    title = str(product.get("title") or product.get("name") or sku)
    orch = get_ai_orchestrator()
    facts = {k:v for k,v in (product or {}).items() if k in ("sku","title","name","brand","category","short_description","sale_price","price","slug","url") and v}
    prompt = (
        "Erzeuge plattformspezifische Social-Commerce-Captions als JSON. Keys: platforms:{name:{caption,hashtags[],cta,short_title,video_script_15s}} "
        "und common:{hook,value_props[],shopware_deeplink_hint}. Nur belegte Fakten, Shopware-Deep-Link-Hinweis nutzen falls slug/url vorhanden. "
        f"Produkt: {json.dumps(facts, ensure_ascii=False)[:4000]}"
    )
    res = await orch.chat_completion(
        messages=[{"role":"system","content":"Du bist Social-Commerce-Redakteur (omni-content-seo). Antworte nur mit JSON."},
                  {"role":"user","content":prompt}],
        module="nexus_social_publish", combo="omni-content-seo", task="social_caption", use_cache=True, temperature=0.4,
        context_text=json.dumps(facts, ensure_ascii=False),
    )
    content = res.get("content") or ""
    parsed = None
    try:
        s=content; i,j=s.find("{"),s.rfind("}")
        if i!=-1 and j>i: parsed=json.loads(s[i:j+1])
    except Exception:
        parsed=None
    plats = platforms or [p for p,c in supported_platforms().items() if c.get("enabled")]
    jobs = []
    for p in plats:
        copy = ((parsed or {}).get("platforms") or {}).get(p) or {}
        payload = {"file": media_url or product.get("media_url") or "", "title": copy.get("short_title") or title, "desc": copy.get("caption") or "", "tags": [str(t).lstrip('#') for t in (copy.get("hashtags") or [])], "note": copy.get("caption") or ""}
        try:
            plan = plan_publish_job(platform=p, action="upload-video" if payload["file"] else "upload-note", account=account, payload=payload)
            jobs.append({"platform": p, "plan_id": plan.id, "status": plan.status, "command": plan.command, "idempotency_key": plan.idempotency_key})
        except Exception as e:
            jobs.append({"platform": p, "error": str(e)})
    out = {"ok": bool(res.get("ok", True)), "sku": sku, "copy": parsed or {"raw": content}, "queued_jobs": jobs, "model": res.get("model"), "privacy_routed": bool(res.get("privacy_routed"))}
    await nexus_event("SocialPublishQueued", {"sku": sku, "jobs": len([j for j in jobs if j.get('plan_id')])}, source="skill_social_publish", entity_type="product", entity_id=sku, correlation_id=correlation_id)
    return out
