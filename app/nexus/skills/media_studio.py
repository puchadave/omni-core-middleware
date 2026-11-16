"""skill_media_studio: product image/video generation briefs/jobs via OmniRoute vision/video combos."""
from __future__ import annotations
import json
from typing import Any
from app.services.ai_orchestrator import get_ai_orchestrator
from app.nexus.event_bus.store import nexus_event

async def skill_media_studio(product: dict[str, Any], *, sku: str = "", mode: str = "image", correlation_id: str = "", **_: Any) -> dict[str, Any]:
    sku = sku or str(product.get("sku") or "")
    title = str(product.get("title") or product.get("name") or sku)
    orch = get_ai_orchestrator()
    combo = "omni-video-gen" if mode == "video" else "omni-vision-photo"
    task = "video_generation_brief" if mode == "video" else "image_generation_brief"
    facts = {k: v for k,v in (product or {}).items() if k in ("sku","title","name","brand","category","material","color","short_description","raw_description","main_image_url","gallery_json") and v}
    prompt = (
        f"Erzeuge einen produktionsreifen {('Kurzvideo' if mode=='video' else 'Produktbild')}-Prompt/Shotplan als JSON für E-Commerce. "
        "Keys: hero_prompt, variants (3), negative_prompt, style_guide, aspect_ratios, scenes, lighting, camera, "
        "product_fidelity_notes, compliance_notes, alt_texts_de. Keine erfundenen Produktmerkmale. "
        f"Produkt: {json.dumps(facts, ensure_ascii=False)[:5000]}"
    )
    res = await orch.chat_completion(
        messages=[{"role":"system","content":"Du bist Creative Director für E-Commerce Media. Antworte nur mit JSON."},
                  {"role":"user","content":prompt}],
        module="nexus_media_studio", combo=combo, task=task, use_cache=False, temperature=0.5,
        context_text=json.dumps(facts, ensure_ascii=False),
    )
    content = (res.get("content") or "")
    parsed = None
    try:
        s=content; i,j=s.find("{"),s.rfind("}")
        if i!=-1 and j>i: parsed=json.loads(s[i:j+1])
    except Exception:
        parsed=None
    out = {"ok": bool(res.get("ok", True)), "sku": sku, "mode": mode, "combo": combo, "creative": parsed or {"raw": content}, "model": res.get("model"), "privacy_routed": bool(res.get("privacy_routed"))}
    await nexus_event("MediaAssetPlanned", {"sku": sku, "mode": mode, "ok": out["ok"]}, source="skill_media_studio", entity_type="product", entity_id=sku, correlation_id=correlation_id)
    return out
