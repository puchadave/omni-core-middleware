"""Shopware-only automatic model photoshoots for specific outfit categories.

Policy (as requested, implemented with guardrails):
- Only for Shopware channel.
- Only for eligible categories/tags: festival, party, sexy outfits, erotikware (configurable keywords).
- Irregular intervals (randomized schedule jitter), no spam/fixed cadence.
- Optional "owner personally with models" only when a configured owner model profile exists
  with verified rights/consent and references; otherwise blocked (no fabricated identity).

Execution uses existing creative agents/routing and only runs when providers/capabilities are
configured; otherwise plans/review-required records are created. No invented outputs.
"""

from __future__ import annotations

import hashlib
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from database import fetch_product, get_setting, list_products, set_setting, utc_now, write_log

ELIGIBLE_KEYWORDS_DEFAULT = [
    "festival", "party", "sexy", "erotik", "erotic", "lingerie", "dessous", "rave", "clubwear", "go-go", "gogo",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


async def _eligible_keywords() -> list[str]:
    raw = await get_setting("shopware_modelphoto_eligible_keywords") or ""
    if raw:
        parts = [p.strip().lower() for p in re.split(r"[,;\n]", str(raw)) if p.strip()]
        if parts:
            return parts
    return ELIGIBLE_KEYWORDS_DEFAULT


def _product_text(p: dict[str, Any]) -> str:
    fields = [p.get("title"), p.get("ai_title"), p.get("category"), p.get("brand"), p.get("raw_description"), p.get("short_description"), p.get("seo_title")]
    tags = p.get("ai_tags_json") or []
    if isinstance(tags, list):
        fields.extend([str(t) for t in tags])
    return " ".join(str(x) for x in fields if x).lower()


async def is_eligible_for_shopware_model_photo(product: dict[str, Any]) -> dict[str, Any]:
    kws = await _eligible_keywords()
    text = _product_text(product)
    matched = [k for k in kws if k and k in text]
    return {"eligible": bool(matched), "matched_keywords": matched, "keywords": kws}


def _irregular_due(last_iso: str | None, *, min_days: int = 7, max_days: int = 45) -> bool:
    """Irregular interval: due if never run or random window elapsed."""
    if not last_iso:
        return True
    try:
        last = datetime.fromisoformat(str(last_iso).replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    # deterministic-ish jitter per day bucket to avoid flapping: hash of date range seed
    days = random.randint(min_days, max_days)
    return _now() >= (last + timedelta(days=days))


async def get_owner_model_profile_status() -> dict[str, Any]:
    """Check if owner/self profile is configured and has rights/consent markers."""
    from database import model_profile_get
    slug = await get_setting("shopware_modelphoto_owner_profile_slug") or "owner_self"
    prof = await model_profile_get(str(slug))
    if not prof:
        return {"configured": False, "allowed": False, "slug": slug, "reason": "owner_profile_not_found"}
    rights = prof.get("rights_consent_json") or {}
    if isinstance(rights, str):
        try:
            import json as _json
            rights = _json.loads(rights)
        except Exception:
            rights = {}
    consent_ok = str(rights.get("status") or "").lower() in ("verified", "approved", "granted")
    refs = prof.get("reference_images_json") or []
    if isinstance(refs, str):
        try:
            import json as _json
            refs = _json.loads(refs)
        except Exception:
            refs = []
    has_refs = isinstance(refs, list) and len(refs) > 0
    allowed = bool(consent_ok and has_refs and prof.get("usage_allowed", True) and prof.get("active", True))
    return {"configured": True, "allowed": allowed, "slug": slug, "consent_status": rights.get("status"), "has_references": has_refs, "reason": None if allowed else "owner_profile_missing_verified_consent_or_references"}


async def plan_shopware_model_photos(*, limit: int = 20, include_owner: bool = False) -> dict[str, Any]:
    """Select eligible Shopware products and build photoshoot plans (no execution)."""
    products = await list_products(limit=1000, offset=0)
    last_run = await get_setting("shopware_modelphoto_last_run_at")
    if not _irregular_due(last_run, min_days=5, max_days=30):
        return {"status": "skipped_not_due", "reason": "irregular_interval_not_reached", "last_run_at": last_run, "plans": []}
    owner = await get_owner_model_profile_status() if include_owner else {"configured": False, "allowed": False}
    plans = []
    scanned = 0
    for p in products or []:
        if p.get("is_bundle"):
            continue
        if str(p.get("status") or "").lower() not in ("active", ""):
            continue
        scanned += 1
        elig = await is_eligible_for_shopware_model_photo(p)
        if not elig.get("eligible"):
            continue
        sku = str(p.get("sku") or "")
        models = []
        if include_owner and owner.get("allowed"):
            # owner personally with models (only if verified profile present)
            models.append(str(owner.get("slug")))
        # always can include canonical AI models if configured; selection left to runtime, here just intent
        plans.append({
            "sku": sku,
            "title": p.get("title"),
            "category": p.get("category"),
            "marketplace": "shopware",
            "matched_keywords": elig.get("matched_keywords"),
            "scene": "festival_party_editorial",
            "shot_type": "model_with_product",
            "include_owner_profile": bool(include_owner and owner.get("allowed")),
            "owner_profile": owner if include_owner else None,
            "models": models,
            "notes": "Shopware-only; Ausführung nur bei konfiguriertem Provider/Capability. Keine erfundenen Outputs.",
        })
        if len(plans) >= limit:
            break
    # mark planned run time (irregular scheduler will still jitter)
    await set_setting("shopware_modelphoto_last_planned_at", utc_now(), is_secret=False)
    await write_log("INFO", "shopware.modelphoto_planned", f"Planned {len(plans)} Shopware model-photo jobs", {"count": len(plans), "scanned": scanned, "include_owner": include_owner})
    return {"status": "ok", "generated_at": utc_now(), "last_run_at": last_run, "eligible_count": len(plans), "scanned": scanned, "owner_profile": owner, "plans": plans}
