"""skill_matterhorn_enrich: SEO texts & attributes via omni-content-seo + knowledge rules (RAG-light)."""
from __future__ import annotations
import json, logging
from pathlib import Path
from typing import Any
from app.services.ai_orchestrator import get_ai_orchestrator, COMBO_CONTENT
from app.nexus.event_bus.store import nexus_event

logger = logging.getLogger(__name__)
KNOWLEDGE_DIRS = [Path("knowledge"), Path("config/knowledge"), Path("docs/knowledge")]

def _load_knowledge_hints(query: str = "", limit: int = 6) -> list[dict[str, str]]:
    hints = []
    q = (query or "").lower()
    for d in KNOWLEDGE_DIRS:
        if not d.exists():
            continue
        for p in sorted(d.glob("**/*"))[:200]:
            if p.suffix.lower() not in {".md",".txt",".json",".yaml",".yml"}: continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")[:4000]
            except Exception:
                continue
            score = sum(1 for w in set(q.split()) if w and w in text.lower()) if q else 1
            if score > 0:
                hints.append({"source": str(p), "excerpt": text[:1200], "score": score})
    hints.sort(key=lambda x: x["score"], reverse=True)
    return hints[:limit]

async def skill_matterhorn_enrich(product: dict[str, Any], *, sku: str = "", locale: str = "de", correlation_id: str = "", **_: Any) -> dict[str, Any]:
    sku = sku or str(product.get("sku") or product.get("id") or "")
    title = str(product.get("title") or product.get("name") or sku)
    hints = _load_knowledge_hints(" ".join([title, str(product.get("brand","")), str(product.get("category","")), str(product.get("material",""))]))
    orch = get_ai_orchestrator()
    facts = {k: v for k, v in (product or {}).items() if v not in (None, "", [], {})}
    prompt = (
        "Erzeuge SEO-optimierte Produktanreicherung als JSON. Nutze NUR belegte Produktdaten und Knowledge-Hinweise; "
        "erfinde keine Zertifikate/GTINs/Eigenschaften. "
        "Keys: seo_title, meta_description, seo_slug, short_description, long_description_html, bullets, "
        "attributes (key/value), tags, faq, schema_org_hints, confidence, sources_used. "
        f"Locale: {locale}. Produkt: {json.dumps(facts, ensure_ascii=False)[:7000]}\n"
        f"Knowledge-Hinweise: {json.dumps(hints, ensure_ascii=False)[:5000]}"
    )
    res = await orch.chat_completion(
        messages=[{"role":"system","content":"Du bist E-Commerce SEO- und Katalog-Enrichment-Agent. Antworte nur mit JSON."},
                  {"role":"user","content":prompt}],
        module="nexus_matterhorn_enrich", combo="omni-content-seo", task="matterhorn_enrich", use_cache=True, temperature=0.2,
        context_text=json.dumps(facts, ensure_ascii=False),
    )
    content = (res.get("content") or "") if isinstance(res, dict) else ""
    parsed = None
    try:
        s = content; i,j = s.find("{"), s.rfind("}")
        if i!=-1 and j>i: parsed = json.loads(s[i:j+1])
    except Exception:
        parsed = None
    out = {"ok": bool(res.get("ok", True)), "sku": sku, "enrichment": parsed or {"raw": content}, "model": res.get("model"), "privacy_routed": bool(res.get("privacy_routed")), "knowledge_sources": [h["source"] for h in hints]}
    await nexus_event("ContentEnriched", {"sku": sku, "ok": out["ok"], "privacy_routed": out["privacy_routed"], "knowledge_count": len(hints)}, source="skill_matterhorn_enrich", entity_type="product", entity_id=sku, correlation_id=correlation_id)
    return out
