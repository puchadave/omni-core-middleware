"""Batch enrichment pipeline for Matterhorn/catalog products.

Async background worker with bounded concurrency. Uses AIOrchestrator:
- omni-content-seo for SEO/HTML descriptions/bullets/meta/category/attribute mapping
- omni-vision-photo (plan-only unless provider capable) for image/freisteller requests
- omni-video-gen (plan-only unless provider capable) for product clips

Progress tracked in batch_jobs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from database import (
    batch_job_create,
    batch_job_get,
    batch_job_update,
    fetch_product,
    list_products,
    upsert_product,
    write_log,
)
from app.services.ai_orchestrator import COMBO_CONTENT, COMBO_VIDEO, COMBO_VISION, get_ai_orchestrator

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _needs_text_enrichment(p: dict[str, Any]) -> bool:
    desc = (p.get("raw_description") or p.get("description") or "").strip()
    ai_desc = (p.get("ai_description_html") or "").strip()
    seo = (p.get("seo_title") or "").strip()
    title = (p.get("ai_title") or "").strip()
    return not desc or not ai_desc or not seo or not title


async def _enrich_one_text(product: dict[str, Any]) -> dict[str, Any]:
    orch = get_ai_orchestrator()
    sku = product.get("sku")
    prompt = (
        "Erzeuge strukturierte Produktdaten als valides JSON mit Feldern: "
        '{"ai_title": str, "seo_title": str, "meta_description": str, "short_description": str, '
        '"ai_description_html": str, "bullet_points": [str], "suggested_category": str, '
        '"attributes": {"key":"value"}}. '
        "Nutze nur vorhandene Produktinfos, erfinde keine technischen Fakten/GTINs. "
        f"Produkt: {json.dumps({k: product.get(k) for k in ('sku','title','brand','category','raw_description','short_description','ean','supplier_price','material') if product.get(k) not in (None,'')}, ensure_ascii=False)[:6000]}"
    )
    res = await orch.chat_completion(
        messages=[{"role": "system", "content": "Du bist E-Commerce-Content- und SEO-Spezialist. Antworte ausschließlich mit JSON."},
                  {"role": "user", "content": prompt}],
        module="content_enrichment",
        combo=COMBO_CONTENT,
        task="seo_content",
        use_cache=True,
        temperature=0.2,
    )
    if not res.get("ok"):
        return {"sku": sku, "status": "error", "error": res.get("error")}
    content = res.get("content") or ""
    data: dict[str, Any] = {}
    try:
        # extract json object
        s = content.strip()
        if "```" in s:
            s = s.split("```", 2)[1]
            if s.startswith("json"):
                s = s[4:]
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end != -1:
            data = json.loads(s[start:end+1])
    except Exception:
        data = {}
    updates: dict[str, Any] = {}
    for fld in ("ai_title", "seo_title", "meta_description", "short_description", "ai_description_html"):
        val = (data.get(fld) or "").strip() if isinstance(data.get(fld), str) else ""
        if val and not (product.get(fld) or "").strip():
            updates[fld] = val[:8000]
    bullets = data.get("bullet_points")
    if isinstance(bullets, list) and bullets and not product.get("ai_tags_json"):
        # store bullets in ai_tags_json if empty (non-destructive repurpose) or keep as-is
        try:
            updates["ai_tags_json"] = [str(b)[:200] for b in bullets[:12]]
        except Exception:
            pass
    if not (product.get("category") or "").strip() and isinstance(data.get("suggested_category"), str):
        updates["category"] = data.get("suggested_category")[:200]
    if updates:
        updates["sku"] = sku
        # preserve required fields for upsert
        updates.setdefault("title", product.get("title") or sku)
        updates.setdefault("supplier_price", product.get("supplier_price") or 0)
        updates.setdefault("stock", product.get("stock") or 0)
        updates.setdefault("status", product.get("status") or "active")
        await upsert_product(**updates)
    return {"sku": sku, "status": "updated" if updates else "skipped", "fields": list(updates.keys()), "privacy_routed": res.get("privacy_routed", False)}


async def run_catalog_enrichment(job_id: str, *, limit: int = 200, only_missing: bool = True, generate_images: bool = False, generate_videos: bool = False, concurrency: int = 5) -> None:
    orch = get_ai_orchestrator()
    await orch.start()
    await batch_job_update(job_id, status="running", started_at=_now())
    await write_log("INFO", "enrichment.started", f"Batch enrichment started {job_id}", {"job_id": job_id})
    try:
        products = await list_products(limit=min(max(limit, 1), 2000), offset=0)
        candidates = [p for p in products if (not only_missing or _needs_text_enrichment(p))]
        await batch_job_update(job_id, total_items=len(candidates))
        sem = asyncio.Semaphore(max(1, min(concurrency, 10)))
        processed = ok = failed = 0
        results: list[dict[str, Any]] = []

        async def worker(p: dict[str, Any]):
            nonlocal processed, ok, failed
            async with sem:
                try:
                    r = await _enrich_one_text(p)
                    # plan-only for media (no fabricated generation without verified provider capability)
                    if generate_images:
                        r["image_plan"] = {"combo": COMBO_VISION, "status": "planned_only"}
                    if generate_videos:
                        r["video_plan"] = {"combo": COMBO_VIDEO, "status": "planned_only"}
                    if r.get("status") == "error":
                        failed += 1
                    else:
                        ok += 1
                    results.append(r)
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    results.append({"sku": p.get("sku"), "status": "error", "error": str(e)[:300]})
                finally:
                    processed += 1
                    if processed % 5 == 0 or processed == len(candidates):
                        await batch_job_update(job_id, processed_items=processed, success_items=ok, failed_items=failed)

        await asyncio.gather(*(worker(p) for p in candidates))
        await batch_job_update(job_id, status="completed", finished_at=_now(), processed_items=processed, success_items=ok, failed_items=failed, result_json={"items": results[:200], "count": len(results)})
        await write_log("INFO", "enrichment.completed", f"Batch enrichment completed {job_id}", {"job_id": job_id, "processed": processed, "ok": ok, "failed": failed})
    except Exception as e:  # noqa: BLE001
        logger.exception("enrichment failed")
        await batch_job_update(job_id, status="failed", finished_at=_now(), error_message=str(e)[:1000])
        await write_log("ERROR", "enrichment.failed", f"Batch enrichment failed {job_id}", {"job_id": job_id, "error": str(e)[:500]})


async def start_catalog_enrichment(*, limit: int = 200, only_missing: bool = True, generate_images: bool = False, generate_videos: bool = False, concurrency: int = 5, created_by: str = "") -> dict[str, Any]:
    job = await batch_job_create(job_type="catalog_enrichment", total_items=0, payload={"limit": limit, "only_missing": only_missing, "generate_images": generate_images, "generate_videos": generate_videos, "concurrency": concurrency}, created_by=created_by)
    job_id = str(job.get("id"))
    asyncio.create_task(run_catalog_enrichment(job_id, limit=limit, only_missing=only_missing, generate_images=generate_images, generate_videos=generate_videos, concurrency=concurrency))
    return {"job_id": job_id, "status": "started"}
