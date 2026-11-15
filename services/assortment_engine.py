"""Automatic AI-assisted marketplace assortment selection and niche analysis.

Selects up to MAX_PER_SHOP (2000) products per marketplace using available signals:
- demand/order velocity (from orders), stock/availability, recency, category/brand diversity
- trend/news keyword relevance (from stored trends/news items), margin/pricing signals
- optional AI niche analysis via AIOrchestrator (omni-content-seo) when configured

No fabricated external/social data is ingested; "social/trend" signals are derived from
stored news/trend data and provided keywords. Results are transparent and auditable.
"""

from __future__ import annotations

import json
import logging
import math
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from database import (
    assortment_plan_save,
    assortment_plan_list,
    list_orders,
    list_products,
    news_list_items,
    write_log,
)

logger = logging.getLogger(__name__)

MAX_PER_SHOP = 2000
DEFAULT_MARKETPLACES = ["shopware", "amazon", "ebay", "google_shopping", "kaufland", "otto"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-ZäöüÄÖÜß0-9]{3,}", _norm(text)))


def _product_text(p: dict[str, Any]) -> str:
    parts = [
        p.get("title"), p.get("ai_title"), p.get("brand"), p.get("category"),
        p.get("raw_description"), p.get("short_description"), p.get("ai_description_html"),
        p.get("seo_title"), p.get("meta_description"),
    ]
    tags = p.get("ai_tags_json") or []
    if isinstance(tags, list):
        parts.extend([str(t) for t in tags])
    return " ".join(str(x) for x in parts if x)


async def _demand_index() -> dict[str, dict[str, float]]:
    """Compute per-SKU demand signals from orders (available data)."""
    try:
        orders = await list_orders(limit=500, offset=0)
    except Exception:
        orders = []
    by_sku: dict[str, dict[str, float]] = defaultdict(lambda: {"orders": 0.0, "qty": 0.0})
    for o in orders or []:
        sku = str(o.get("sku") or "").strip()
        if not sku:
            continue
        qty = float(o.get("quantity") or 1)
        by_sku[sku]["orders"] += 1.0
        by_sku[sku]["qty"] += qty
    return by_sku


async def _trend_tokens() -> tuple[set[str], list[dict[str, Any]]]:
    """Derive trend keywords from stored news/trend items (no external scraping)."""
    items = []
    try:
        items = await news_list_items(limit=200)
    except Exception:
        items = []
    tokens: set[str] = set()
    signals = []
    for it in items or []:
        text = " ".join([str(it.get("title") or ""), str(it.get("summary") or ""), str(it.get("category") or "")])
        toks = _tokenize(text)
        tokens |= toks
        signals.append({"id": it.get("id"), "title": it.get("title"), "category": it.get("category"), "source": it.get("source_id")})
    return tokens, signals[:50]


def _margin_signal(p: dict[str, Any]) -> float:
    sale = float(p.get("sale_price") or p.get("price") or 0)
    cost = float(p.get("supplier_price") or p.get("purchase_price") or 0)
    if sale > 0 and cost >= 0 and sale >= cost:
        return min((sale - cost) / sale, 1.0)
    if sale > 0:
        return 0.2
    return 0.0


async def _ai_niche_analysis(marketplace: str, candidates: list[dict[str, Any]], trend_signals: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Optional AI niche analysis via orchestrator; returns structured hints or None."""
    try:
        from app.services.ai_orchestrator import COMBO_CONTENT, get_ai_orchestrator
        orch = get_ai_orchestrator()
        sample = []
        for p in candidates[:40]:
            sample.append({k: p.get(k) for k in ("sku", "title", "brand", "category", "sale_price", "supplier_price", "stock", "demand_qty") if p.get(k) not in (None, "")})
        prompt = (
            "Analysiere Nischen und Sortimentschwerpunkte für den Marktplatz basierend ausschließlich auf den gelieferten Produkt-/Signal-Daten. "
            "Erfinde keine externen Marktdaten. Antworte als JSON: "
            '{"niches":[{"name":str,"rationale":str,"focus_categories":[str],"focus_keywords":[str]}],'
            '"priority_keywords":[str],"notes":str}. '
            f"Marktplatz: {marketplace}. Trend/News-Signale (intern): {json.dumps(trend_signals[:20], ensure_ascii=False)[:3000]}. "
            f"Produktauswahl-Stichprobe: {json.dumps(sample, ensure_ascii=False)[:4000]}"
        )
        res = await orch.chat_completion(
            messages=[{"role": "system", "content": "Du bist E-Commerce-Sortiments- und Nischenanalyst. Antworte nur mit JSON."},
                      {"role": "user", "content": prompt}],
            module="assortment_niche",
            combo=COMBO_CONTENT,
            task="niche_analysis",
            use_cache=True,
            temperature=0.2,
        )
        if not res.get("ok"):
            return None
        content = res.get("content") or ""
        s = content.strip()
        if "```" in s:
            for part in s.split("```"):
                part = part.strip()
                if part.lower().startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    s = part; break
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end != -1:
            return json.loads(s[start:end+1])
    except Exception as e:
        logger.debug("ai niche analysis unavailable: %s", e)
    return None


async def generate_marketplace_assortment(marketplace: str, *, max_items: int = MAX_PER_SHOP, use_ai_niche: bool = True, only_active: bool = True) -> dict[str, Any]:
    marketplace = (marketplace or "shopware").strip().lower()
    max_items = min(max(int(max_items or MAX_PER_SHOP), 1), MAX_PER_SHOP)
    products = await list_products(limit=5000, offset=0)
    demand = await _demand_index()
    trend_toks, trend_signals = await _trend_tokens()

    scored: list[dict[str, Any]] = []
    for p in products or []:
        if p.get("is_bundle"):
            continue
        status = str(p.get("status") or "").lower()
        if only_active and status not in ("active", ""):
            continue
        sku = str(p.get("sku") or "")
        stock = int(p.get("stock_quantity") or p.get("stock") or 0)
        d = demand.get(sku, {"orders": 0.0, "qty": 0.0})
        text = _product_text(p)
        toks = _tokenize(text)
        trend_overlap = len(toks & trend_toks)
        # scoring (transparent): demand + stock + trend relevance + margin + completeness
        score = 0.0
        score += min(d.get("qty", 0.0), 200.0) * 0.5
        score += min(d.get("orders", 0.0), 50.0) * 1.0
        score += min(stock, 500.0) * 0.05
        score += min(trend_overlap, 10) * 1.2
        score += _margin_signal(p) * 10.0
        # completeness
        if (p.get("main_image_url") or "").strip():
            score += 2.0
        if (p.get("raw_description") or p.get("ai_description_html") or "").strip():
            score += 1.5
        if (p.get("ean") or p.get("barcode") or "").strip():
            score += 0.5
        scored.append({
            "sku": sku,
            "title": p.get("title") or p.get("ai_title") or sku,
            "brand": p.get("brand"),
            "category": p.get("category"),
            "stock": stock,
            "demand_qty": d.get("qty", 0.0),
            "demand_orders": d.get("orders", 0.0),
            "trend_keyword_hits": trend_overlap,
            "margin_signal": round(_margin_signal(p), 4),
            "score": round(score, 4),
            "main_image_url": p.get("main_image_url") or "",
            "reasons": [r for r, ok in [
                ("nachfrage", d.get("qty", 0) > 0), ("lagerbestand", stock > 0),
                ("trend_news_relevanz", trend_overlap > 0), ("marge", _margin_signal(p) > 0),
                ("bild_vorhanden", bool((p.get("main_image_url") or "").strip())),
            ] if ok],
        })

    scored.sort(key=lambda x: (x["score"], x["demand_qty"], x["stock"]), reverse=True)
    selected = scored[:max_items]

    # niche analysis
    cat_counts = Counter([s.get("category") or "uncategorized" for s in selected])
    brand_counts = Counter([s.get("brand") or "unbekannt" for s in selected])
    niches = [
        {"name": cat, "count": cnt, "share": round(cnt / max(len(selected), 1), 4), "rationale": "Kategorieanteil im ausgewählten Sortiment (datenbasiert)"}
        for cat, cnt in cat_counts.most_common(10)
    ]
    niche_ai = await _ai_niche_analysis(marketplace, selected, trend_signals) if use_ai_niche and selected else None

    plan = {
        "id": str(uuid.uuid4()),
        "marketplace": marketplace,
        "generated_at": _now(),
        "max_items": max_items,
        "selected_count": len(selected),
        "candidate_count": len(scored),
        "selection_policy": {
            "signals": ["nachfrage_bestellungen", "lagerbestand", "trend_news_keywords_intern", "marge_signal", "datenvollstaendigkeit"],
            "note": "Keine externen Social-Media-Daten erfunden; Trend-Signale aus gespeicherten News/Trends. Max 2000 pro Shop.",
        },
        "niche_analysis": {
            "top_categories": niches,
            "top_brands": [{"name": b, "count": c} for b, c in brand_counts.most_common(10)],
            "ai": niche_ai,
        },
        "trend_signals_used": trend_signals[:20],
        "items": selected,
    }
    try:
        await assortment_plan_save(plan)
    except Exception as e:
        logger.warning("could not persist assortment plan: %s", e)
    await write_log("INFO", "assortment.generated", f"Assortment generated for {marketplace}: {len(selected)} items", {"marketplace": marketplace, "count": len(selected)})
    return plan


async def generate_all_marketplace_assortments(marketplaces: list[str] | None = None, *, max_items: int = MAX_PER_SHOP, use_ai_niche: bool = True) -> dict[str, Any]:
    mps = marketplaces or DEFAULT_MARKETPLACES
    results = {}
    for mp in mps:
        try:
            results[mp] = await generate_marketplace_assortment(mp, max_items=max_items, use_ai_niche=use_ai_niche)
        except Exception as e:
            results[mp] = {"marketplace": mp, "error": str(e), "selected_count": 0}
    return {"generated_at": _now(), "max_items": max_items, "marketplaces": results}
