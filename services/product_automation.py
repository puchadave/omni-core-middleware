"""Automatic AI-supported product automation (guardrailed).

- Per product: generate 3 different model photos and add to product card alongside originals.
- Every 4 weeks: generate/add 3-photo set per product based on current events/festivals (scene news).
- Every 14 days: refresh/update product texts & SEO per marketplace.
- Owner/self model photos can be included only when a verified/consented owner profile exists.

All generation/execution uses configured providers/capabilities only; no fabricated outputs.
Original/Matterhorn media is never deleted/overwritten.
"""

from __future__ import annotations

import json
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from database import (
    append_product_media,
    fetch_product,
    get_connection,
    get_pg_pool,
    get_setting,
    list_products,
    news_list_items,
    set_setting,
    upsert_product,
    uses_postgres,
    utc_now,
    write_log,
)

logger = logging.getLogger(__name__)


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now_dt()).isoformat(timespec="seconds")


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


async def _scene_event_context(limit: int = 8) -> dict[str, Any]:
    try:
        items = await news_list_items(limit=60)
    except Exception:
        items = []
    # prefer event/festival/scene keywords
    kw = ("festival", "event", "lineup", "party", "weekend", "hardcore", "uptempo", "hardstyle", "gabber", "thunderdome", "dominator", "defqon", "masters", "q-dance")
    picked = []
    for it in items or []:
        text = f"{it.get('title','')} {it.get('summary','')} {it.get('category','')}".lower()
        if any(k in text for k in kw):
            picked.append({"title": it.get("title"), "summary": (it.get("summary") or "")[:300], "url": it.get("url"), "category": it.get("category")})
        if len(picked) >= limit:
            break
    if not picked:
        picked = [{"title": it.get("title"), "summary": (it.get("summary") or "")[:200], "url": it.get("url")} for it in (items or [])[:limit]]
    themes = [p.get("title") for p in picked if p.get("title")]
    return {"events": picked, "themes": themes, "generated_at": _iso()}


async def _owner_profile_allowed() -> dict[str, Any]:
    slug = await get_setting("shopware_modelphoto_owner_profile_slug") or await get_setting("owner_model_profile_slug") or "owner_self"
    try:
        from database import model_profile_get
        prof = await model_profile_get(str(slug))
    except Exception:
        prof = None
    if not prof:
        return {"allowed": False, "slug": slug, "reason": "owner_profile_not_found"}
    rights = prof.get("rights_consent_json") or {}
    if isinstance(rights, str):
        try:
            rights = json.loads(rights)
        except Exception:
            rights = {}
    consent_ok = str(rights.get("status") or "").lower() in ("verified", "approved", "granted")
    refs = prof.get("reference_images_json") or []
    if isinstance(refs, str):
        try:
            refs = json.loads(refs)
        except Exception:
            refs = []
    has_refs = isinstance(refs, list) and len(refs) > 0
    allowed = bool(consent_ok and has_refs and prof.get("usage_allowed", True) and prof.get("active", True))
    return {"allowed": allowed, "slug": slug, "consent_status": rights.get("status"), "has_references": has_refs, "reason": None if allowed else "missing_verified_consent_or_references"}


async def _collect_generated_assets_from_outputs(outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    media = []
    for o in outputs or []:
        asset = o.get("asset") or {}
        gen = o.get("generation") or {}
        url = (asset.get("url") or gen.get("image_url") or "").strip()
        if not url:
            # no fabricated/local placeholder materialized -> skip
            continue
        media.append({
            "url": url,
            "media_type": "image",
            "title": f"AI Model Photo {o.get('variant','')}".strip(),
            "alt_text": "AI-generiertes Model-Foto mit Produkt (automatisch)",
            "metadata_json": {
                "source": "ai_model_photoshoot",
                "generated_at": _iso(),
                "asset_id": asset.get("id"),
                "job_generation": {k: gen.get(k) for k in ("provider", "model", "seed") if gen.get(k) is not None},
            },
        })
    return media


async def generate_model_photos_for_product(product: dict[str, Any], *, count: int = 3, context: dict[str, Any] | None = None, include_owner: bool = True, scene: str = "studio_event") -> dict[str, Any]:
    """Generate `count` different model photos for a product and append to product card (with originals kept)."""
    sku = str(product.get("sku") or "")
    title = product.get("title") or product.get("ai_title") or sku
    category = product.get("category") or ""
    brand = product.get("brand") or ""
    ctx = context or {}
    themes = ctx.get("themes") or []
    theme_text = "; ".join([str(t) for t in themes[:5]]) if themes else "aktuelle Szene/Festival-Atmosphäre"
    owner = await _owner_profile_allowed() if include_owner else {"allowed": False}
    include_owner_flag = bool(include_owner and owner.get("allowed"))

    # build varied prompts (3 different shots)
    base = f"Professionelles Model-Fotoshooting mit Produkt '{title}' ({brand}, {category}). Produktdetails getreu Original, keine erfundenen Logos/Details."
    variants_spec = [
        {"shot_type": "full_body", "pose": "dynamic", "lighting": "stage", "camera": "35mm"},
        {"shot_type": "three_quarter", "pose": "confident", "lighting": "festival", "camera": "50mm"},
        {"shot_type": "close_detail", "pose": "natural", "lighting": "studio_soft", "camera": "85mm"},
    ]
    results = []
    added_media_all: list[dict[str, Any]] = []
    errors = []
    try:
        from services.ai_photoshooting import ProductPhotographyAgent
    except Exception as e:
        return {"sku": sku, "status": "unavailable", "error": f"photoshoot_agent_unavailable: {e}", "added": 0}

    for i in range(max(1, min(count, 6))):
        spec = variants_spec[i % len(variants_spec)]
        prompt_ctx = f"Kontext/Events: {theme_text}. Szene: {scene}. Variante {i+1}/{count}, Shot: {spec['shot_type']}."
        models = [owner.get("slug")] if include_owner_flag else []
        try:
            plan = await ProductPhotographyAgent.plan(
                sku,
                models=models,
                scene=scene,
                shot_type=spec["shot_type"],
                variants=1,
                pose=spec["pose"],
                camera=spec["camera"],
                lighting=spec["lighting"],
                aspect_ratio="4:5",
                runtime=None,
                extra={"event_context": ctx, "prompt_hint": prompt_ctx, "include_owner": include_owner_flag},
            )
            exec_res = await ProductPhotographyAgent.execute_plan(plan, actor="automation", project_id=None, runtime=None)
            outputs = exec_res.get("outputs") or []
            media = await _collect_generated_assets_from_outputs(outputs)
            if media:
                added = await append_product_media(sku, media, source="ai_model_photoshoot")
                added_media_all.extend(media)
            else:
                added = 0
            results.append({
                "variant": i + 1,
                "status": exec_res.get("status"),
                "job_id": (exec_res.get("job") or {}).get("id"),
                "added": added,
                "reason": None if media else "no_materialized_output_or_provider_not_configured",
            })
        except Exception as e:
            logger.exception("photo generation failed for %s variant %s", sku, i + 1)
            errors.append(str(e))
            results.append({"variant": i + 1, "status": "error", "error": str(e), "added": 0})

    await write_log("INFO", "automation.photos_generated", f"Generated model photos for {sku}: added {len(added_media_all)}", {"sku": sku, "added": len(added_media_all), "include_owner": include_owner_flag})
    return {"sku": sku, "status": "ok", "requested": count, "added": len(added_media_all), "include_owner": include_owner_flag, "owner_profile": owner, "results": results, "errors": errors, "event_context": ctx}


async def run_initial_model_photos(*, limit_products: int = 50, count: int = 3, include_owner: bool = True) -> dict[str, Any]:
    products = await list_products(limit=max(limit_products, 1), offset=0)
    ctx = await _scene_event_context()
    out = []
    for p in (products or []):
        if p.get("is_bundle"):
            continue
        out.append(await generate_model_photos_for_product(p, count=count, context=ctx, include_owner=include_owner, scene="studio_event"))
        if len(out) >= limit_products:
            break
    return {"generated_at": _iso(), "products_processed": len(out), "count_per_product": count, "context": ctx, "results": out}


async def run_4week_event_photo_refresh(*, limit_products: int = 50, count: int = 3, include_owner: bool = True) -> dict[str, Any]:
    last = await get_setting("automation_photo_4w_last_run_at")
    last_dt = _parse_dt(last)
    # 4 weeks
    if last_dt and _now_dt() < (last_dt + timedelta(days=28)):
        return {"status": "skipped_not_due", "last_run_at": last, "next_due_after_days": 28}
    ctx = await _scene_event_context(limit=10)
    products = await list_products(limit=max(limit_products, 1), offset=0)
    out = []
    for p in (products or []):
        if p.get("is_bundle"):
            continue
        out.append(await generate_model_photos_for_product(p, count=count, context=ctx, include_owner=include_owner, scene="event_festival"))
    await set_setting("automation_photo_4w_last_run_at", _iso(), is_secret=False)
    await set_setting("automation_photo_4w_last_context", json.dumps(ctx, ensure_ascii=False, default=str), is_secret=False)
    return {"status": "ok", "generated_at": _iso(), "products_processed": len(out), "count_per_product": count, "context": ctx, "results": out}


async def run_14day_text_refresh(*, limit_products: int = 100, marketplaces: list[str] | None = None) -> dict[str, Any]:
    last = await get_setting("automation_text_14d_last_run_at")
    last_dt = _parse_dt(last)
    if last_dt and _now_dt() < (last_dt + timedelta(days=14)):
        return {"status": "skipped_not_due", "last_run_at": last, "next_due_after_days": 14}
    mps = marketplaces or ["shopware", "amazon", "ebay", "google_shopping", "kaufland", "otto"]
    products = await list_products(limit=max(limit_products, 1), offset=0)
    refreshed = 0
    errors = []
    try:
        from services.content_engine import generate_and_store
        from services.marketplace_content import upsert_marketplace_content
    except Exception as e:
        return {"status": "unavailable", "error": str(e)}
    for p in (products or []):
        if p.get("is_bundle"):
            continue
        try:
            result = await generate_and_store(p, mps, omniroute_client=None, model=None)
            for it in (result.get("items") or []):
                await upsert_marketplace_content({
                    "product_sku": str(p.get("sku") or ""),
                    "marketplace": str(it.get("channel") or "shopware"),
                    "title": it.get("title") or "",
                    "short_description": it.get("short_description") or "",
                    "description": it.get("description") or "",
                    "seo_title": it.get("seo_title") or "",
                    "meta_description": it.get("meta_description") or "",
                    "keywords": it.get("keywords") or [],
                    "bullets": it.get("bullets") or [],
                    "status": "draft",
                    "approval_status": "draft",
                    "last_generated_at": _iso(),
                    "metadata": {"source": "automation_text_refresh_14d", "model": it.get("model"), "validation": it.get("validation")},
                })
            refreshed += 1
        except Exception as e:
            errors.append({"sku": p.get("sku"), "error": str(e)})
    await set_setting("automation_text_14d_last_run_at", _iso(), is_secret=False)
    await write_log("INFO", "automation.text_refreshed", f"14-day text refresh completed: {refreshed} products", {"count": refreshed})
    return {"status": "ok", "generated_at": _iso(), "products_refreshed": refreshed, "marketplaces": mps, "errors": errors[:20]}
