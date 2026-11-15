"""Multi-channel AI content engine: SEO optimization and marketplace-specific product descriptions.

Generates platform/marketplace-specific drafts from canonical/enriched product data,
validates basic constraints, versions outputs, and enforces approval-before-export.
No invented facts/certifications/GTINs; uses only known product data.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from database import content_version_create, content_version_list

logger = logging.getLogger(__name__)

# Marketplace/channel profiles: limits, tone/style hints, SEO focus
PLATFORM_PROFILES: dict[str, dict[str, Any]] = {
    "shopware": {"title_max": 255, "short_max": 500, "desc_max": 20000, "html": True, "bullets": True, "tone": "informativ, markenkonform, verkaufsstark", "seo": "stark"},
    "shopware6": {"title_max": 255, "short_max": 500, "desc_max": 20000, "html": True, "bullets": True, "tone": "informativ, markenkonform, verkaufsstark", "seo": "stark"},
    "ebay": {"title_max": 80, "short_max": 500, "desc_max": 40000, "html": True, "bullets": True, "tone": "klar, vertrauensbildend, kaufanregend", "seo": "mittel"},
    "amazon": {"title_max": 200, "short_max": 2000, "desc_max": 2000, "html": False, "bullets": True, "tone": "prägnant, benefit-orientiert, suchwortoptimiert", "seo": "sehr stark"},
    "google_shopping": {"title_max": 150, "short_max": 5000, "desc_max": 5000, "html": False, "bullets": False, "tone": "faktisch, exakt, attributreich", "seo": "sehr stark"},
    "kaufland": {"title_max": 100, "short_max": 500, "desc_max": 25000, "html": True, "bullets": True, "tone": "klar, informativ, serviceorientiert", "seo": "mittel"},
    "otto": {"title_max": 100, "short_max": 500, "desc_max": 20000, "html": True, "bullets": True, "tone": "hochwertig, kundenorientiert, präzise", "seo": "mittel"},
    "hood": {"title_max": 80, "short_max": 500, "desc_max": 20000, "html": True, "bullets": True, "tone": "klar, direkt", "seo": "mittel"},
    "zalando": {"title_max": 120, "short_max": 500, "desc_max": 5000, "html": False, "bullets": True, "tone": "modisch, präzise, markenbewusst", "seo": "mittel"},
    "social": {"title_max": 120, "short_max": 2200, "desc_max": 2200, "html": False, "bullets": True, "tone": "kurz, aufmerksamkeitsstark", "seo": "niedrig"},
    "website": {"title_max": 255, "short_max": 500, "desc_max": 20000, "html": True, "bullets": True, "tone": "informativ, SEO-optimiert", "seo": "stark"},
}


def _safe_text(v: Any, limit: int = 5000) -> str:
    s = str(v or "").strip()
    return s[:limit]


def _json_obj_from_text(text: str) -> dict[str, Any]:
    if not text:
        return {}
    s = text.strip()
    # strip code fences
    if "```" in s:
        parts = s.split("```")
        for p in parts:
            p = p.strip()
            if p.lower().startswith("json"):
                p = p[4:].strip()
            if p.startswith("{") and p.endswith("}"):
                s = p
                break
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except Exception:
            return {}
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _slugify(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[ä]", "ae", t)
    t = re.sub(r"[ö]", "oe", t)
    t = re.sub(r"[ü]", "ue", t)
    t = re.sub(r"[ß]", "ss", t)
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:120] or "produkt"


def _known_product_facts(product: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "sku", "title", "name", "ai_title", "brand", "manufacturer", "category",
        "color", "material", "size", "ean", "gtin", "barcode",
        "supplier_price", "purchase_price", "price", "sale_price",
        "raw_description", "description", "short_description", "ai_description_html",
        "weight_net_kg", "weight_gross_kg", "length_cm", "width_cm", "height_cm",
        "country_of_origin", "hs_code", "attributes", "properties", "variants",
        "seo_title", "meta_description", "slug",
    ]
    facts: dict[str, Any] = {}
    for k in keys:
        v = product.get(k)
        if v not in (None, "", [], {}):
            facts[k] = v
    return facts


def validate_platform_content(platform: str, content: dict[str, Any]) -> dict[str, Any]:
    profile = PLATFORM_PROFILES.get(platform, PLATFORM_PROFILES.get("shopware", {}))
    issues = []
    title = _safe_text(content.get("title") or content.get("seo_title") or "")
    desc = _safe_text(content.get("description") or content.get("long_description") or "", 50000)
    meta = _safe_text(content.get("meta_description") or "", 2000)
    if len(title) > profile.get("title_max", 255):
        issues.append(f"title_too_long>{profile['title_max']}")
    if len(desc) > profile.get("desc_max", 20000):
        issues.append(f"description_too_long>{profile['desc_max']}")
    if meta and len(meta) > 160:
        issues.append("meta_description_too_long>160")
    if not profile.get("html", True):
        if re.search(r"<[^>]+>", desc):
            issues.append("html_not_allowed")
    # basic unverified-claim guard
    if re.search(r"\b(zertifiziert|garantiert|medizinisch|heilend|testsieger|klinisch)\b", desc.lower()):
        issues.append("potential_unverified_claim_manual_review")
    return {"valid": len([i for i in issues if not i.startswith("potential_")]) == 0, "issues": issues, "profile": profile}


def _build_fallback_draft(product: dict[str, Any], channel: str) -> dict[str, Any]:
    facts = _known_product_facts(product)
    profile = PLATFORM_PROFILES.get(channel, PLATFORM_PROFILES.get("shopware", {}))
    base_title = _safe_text(facts.get("ai_title") or facts.get("title") or facts.get("name") or facts.get("sku") or "Produkt")
    brand = _safe_text(facts.get("brand") or facts.get("manufacturer") or "")
    category = _safe_text(facts.get("category") or "")
    base_desc = _safe_text(facts.get("raw_description") or facts.get("description") or facts.get("ai_description_html") or "", 4000)
    short = _safe_text(facts.get("short_description") or base_desc, profile.get("short_max", 500))
    # attribute bullets from known facts only
    attrs = []
    for label, key in [("Marke", "brand"), ("Hersteller", "manufacturer"), ("Kategorie", "category"), ("Farbe", "color"), ("Material", "material"), ("Größe", "size"), ("EAN/GTIN", "ean"), ("Herkunft", "country_of_origin")]:
        val = facts.get(key) or (facts.get("gtin") if key == "ean" and not facts.get("ean") else None)
        if val:
            attrs.append(f"{label}: {_safe_text(val, 120)}")
    title_bits = [b for b in [base_title, brand, category] if b]
    seo_title = _safe_text(" | ".join(title_bits[:3]), 70) or base_title[:70]
    # marketplace-individualized, fact-based description
    intro = f"{base_title}"
    if brand:
        intro += f" von {brand}"
    if category:
        intro += f" in der Kategorie {category}"
    intro += "."
    desc_parts = [intro]
    if base_desc:
        desc_parts.append(base_desc)
    if attrs:
        if profile.get("html", True):
            desc_parts.append("<ul>" + "".join(f"<li>{a}</li>" for a in attrs[:10]) + "</ul>")
        else:
            desc_parts.append("Merkmale: " + "; ".join(attrs[:10]))
    desc_parts.append("Angaben basieren auf den vorliegenden Produktdaten; nicht belegte Eigenschaften werden nicht ergänzt.")
    description = "\n".join([p for p in desc_parts if p]).strip()
    meta_desc = _safe_text((base_desc or intro), 155)
    keywords = [x for x in [base_title, brand, category, _safe_text(facts.get("color") or ""), _safe_text(facts.get("material") or "")] if x]
    # de-dup preserve order
    seen = set(); kw_out = []
    for k in keywords:
        kl = k.lower()
        if kl and kl not in seen:
            seen.add(kl); kw_out.append(k)
    return {
        "channel": channel,
        "title": _safe_text(base_title, profile.get("title_max", 255)),
        "short_description": short or _safe_text(intro, profile.get("short_max", 500)),
        "description": _safe_text(description, profile.get("desc_max", 20000)),
        "bullets": attrs[:10] or ["Produktdaten gemäß vorliegender Quelle"],
        "seo_title": seo_title[:70],
        "meta_description": meta_desc[:160],
        "url_slug": _slugify(f"{base_title}-{brand}-{channel}"),
        "keywords": kw_out[:12],
        "facts_used": facts,
        "generated_by": "template_fallback",
        "model": "",
        "approval_status": "draft",
        "marketplace_individualized": True,
    }


async def _generate_with_ai(product: dict[str, Any], channel: str, model: str | None = None) -> dict[str, Any] | None:
    """Try AI generation via AIOrchestrator (omni-content-seo) with guardrails."""
    facts = _known_product_facts(product)
    profile = PLATFORM_PROFILES.get(channel, PLATFORM_PROFILES.get("shopware", {}))
    prompt = (
        "Erzeuge marktplatz-individualisierten Produktcontent und SEO als valides JSON. "
        "Keys: title, short_description, description, bullets, seo_title, meta_description, keywords, url_slug. "
        "Regeln: Nutze ausschließlich die belegten Produktdaten/Fakten; erfinde KEINE Eigenschaften, Maße, Materialien, "
        "Zertifikate, Testergebnisse, Garantien, Kompatibilitäten oder GTINs. "
        "Wenn Information fehlt, lasse sie weg oder formuliere neutral ohne Behauptung. "
        f"Marktplatz/Kanal: {channel}. Stil/Ton: {profile.get('tone','neutral')}. SEO-Fokus: {profile.get('seo','mittel')}. "
        f"Limits: title<={profile.get('title_max',255)}, meta_description<=160, short_description<={profile.get('short_max',500)}, description<={profile.get('desc_max',20000)}. "
        f"HTML erlaubt: {profile.get('html', True)}. Bullets erlaubt: {profile.get('bullets', True)}. "
        f"Belegte Fakten: {json.dumps(facts, ensure_ascii=False)[:7000]}"
    )
    # Prefer central AIOrchestrator
    try:
        from app.services.ai_orchestrator import COMBO_CONTENT, get_ai_orchestrator
        orch = get_ai_orchestrator()
        res = await orch.chat_completion(
            messages=[
                {"role": "system", "content": "Du bist E-Commerce-SEO- und Marktplatz-Content-Spezialist. Antworte ausschließlich mit JSON."},
                {"role": "user", "content": prompt},
            ],
            module="content_seo",
            combo=COMBO_CONTENT,
            task="seo_marketplace_content",
            use_cache=True,
            temperature=0.2,
        )
        if res.get("ok"):
            parsed = _json_obj_from_text(res.get("content") or "")
            if parsed:
                parsed["_generated_by"] = "omni-content-seo"
                parsed["_model"] = str(res.get("model") or model or COMBO_CONTENT)
                parsed["_privacy_routed"] = bool(res.get("privacy_routed", False))
                return parsed
    except Exception as e:
        logger.debug("orchestrator content gen unavailable: %s", e)
    return None


def _merge_ai_into_draft(draft: dict[str, Any], ai: dict[str, Any], channel: str) -> dict[str, Any]:
    profile = PLATFORM_PROFILES.get(channel, PLATFORM_PROFILES.get("shopware", {}))
    for k in ["title", "short_description", "description", "seo_title", "meta_description", "url_slug"]:
        v = ai.get(k)
        if isinstance(v, str) and v.strip():
            draft[k] = v.strip()
    for k in ["bullets", "keywords"]:
        v = ai.get(k)
        if isinstance(v, list) and v:
            draft[k] = [str(x).strip() for x in v if str(x).strip()][:12]
    # enforce limits softly
    draft["title"] = _safe_text(draft.get("title"), profile.get("title_max", 255))
    draft["short_description"] = _safe_text(draft.get("short_description"), profile.get("short_max", 500))
    draft["description"] = _safe_text(draft.get("description"), profile.get("desc_max", 20000))
    draft["seo_title"] = _safe_text(draft.get("seo_title") or draft.get("title"), 70)
    draft["meta_description"] = _safe_text(draft.get("meta_description"), 160)
    if draft.get("url_slug"):
        draft["url_slug"] = _slugify(str(draft.get("url_slug")))
    draft["generated_by"] = ai.get("_generated_by") or "omniroute"
    draft["model"] = ai.get("_model") or draft.get("model") or ""
    draft["privacy_routed"] = bool(ai.get("_privacy_routed", False))
    return draft


async def generate_channel_draft(product: dict[str, Any], channel: str, model: str | None = None, omniroute_client: Any = None) -> dict[str, Any]:
    """Generate individualized SEO + description draft for one marketplace/channel."""
    ch = (channel or "shopware").strip().lower()
    draft = _build_fallback_draft(product, ch)
    ai = await _generate_with_ai(product, ch, model=model)
    # legacy client fallback if orchestrator unavailable but client passed
    if ai is None and omniroute_client is not None:
        try:
            facts = _known_product_facts(product)
            profile = PLATFORM_PROFILES.get(ch, PLATFORM_PROFILES.get("shopware", {}))
            prompt = (
                "Erzeuge plattformgerechten Produktcontent als JSON mit keys title, short_description, description, bullets, seo_title, meta_description, keywords, url_slug. "
                "Nutze ausschließlich belegte Fakten, erfinde keine Eigenschaften/Zertifikate/Claims/GTINs. "
                f"Kanal: {ch}, Ton: {profile.get('tone')}, SEO: {profile.get('seo')}. Fakten: {json.dumps(facts, ensure_ascii=False)[:6000]}"
            )
            resp = None
            if hasattr(omniroute_client, "chat_completions"):
                resp = await omniroute_client.chat_completions(messages=[{"role": "user", "content": prompt}], model=model)
            elif hasattr(omniroute_client, "generate_text"):
                resp = await omniroute_client.generate_text(prompt=prompt, model=model)
            if resp:
                text = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)
                ai = _json_obj_from_text(text)
                if ai:
                    ai["_generated_by"] = "omniroute_client"
        except Exception:
            ai = None
    if ai:
        draft = _merge_ai_into_draft(draft, ai, ch)
    draft["validation"] = validate_platform_content(ch, draft)
    draft["marketplace"] = ch
    draft["channel"] = ch
    return draft


async def generate_and_store(product: dict[str, Any], channels: list[str], omniroute_client: Any = None, model: str | None = None) -> dict[str, Any]:
    """Generate and persist per-marketplace drafts (versioned, approval_status=draft)."""
    # normalize/dedup channels
    norm: list[str] = []
    seen = set()
    for ch in (channels or ["shopware"]):
        c = str(ch or "").strip().lower()
        if not c or c in seen:
            continue
        seen.add(c)
        norm.append(c)
    if not norm:
        norm = ["shopware"]
    results = []
    for ch in norm:
        d = await generate_channel_draft(product, ch, model=model, omniroute_client=omniroute_client)
        try:
            vid = await content_version_create(
                product_sku=str(product.get("sku") or ""),
                channel=ch,
                content=d,
                status="draft",
                model=str(d.get("model") or ""),
                prompt_version="v2-marketplace-seo",
            )
        except Exception as e:
            logger.exception("content version store failed: %s", e)
            vid = None
        d["version_id"] = vid
        results.append(d)
    return {"sku": product.get("sku"), "count": len(results), "items": results, "channels": norm}
