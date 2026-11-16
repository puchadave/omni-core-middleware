"""Advertising & Social Publishing integration for OMNI-CORE.

Adapts concepts from dreammis/social-auto-upload (CLI-first, per-account cookies,
headless browser uploaders, platform skills) into middleware-native orchestration:
- AI copy/hashtag/creative brief generation via OmniRoute (AIOrchestrator, auto/smart-first)
- campaign/publish job modeling with auditability and idempotency keys
- pluggable executor: local `sau` CLI bridge (preferred) or external worker webhook
- no credential storage in DB; account refs point to managed cookie/session stores

This module is safe-by-default: it plans/drafts and records jobs; actual publish
requires explicit execute + configured executor + valid platform account sessions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shlex
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.services.ai_orchestrator import get_ai_orchestrator

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "integrations" / "social_auto_upload.json"

PLATFORM_SPECS: dict[str, dict[str, Any]] = {
    "douyin": {"kind": "video+note", "video_fields": ["title", "desc", "tags"], "note_fields": ["title", "note", "tags"], "region": "CN"},
    "bilibili": {"kind": "video", "video_fields": ["title", "desc", "tid", "tags"], "region": "CN"},
    "xiaohongshu": {"kind": "video+note", "video_fields": ["title", "desc", "tags"], "note_fields": ["title", "note", "tags"], "region": "CN"},
    "kuaishou": {"kind": "video+note", "video_fields": ["title", "desc", "tags"], "note_fields": ["title", "note", "tags"], "region": "CN"},
    "tencent": {"kind": "video", "video_fields": ["title", "desc", "tags"], "region": "CN"},
    "tiktok": {"kind": "video", "video_fields": ["title", "desc", "tags"], "region": "GLOBAL"},
    "baijiahao": {"kind": "video", "video_fields": ["title", "desc", "tags"], "region": "CN"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to read social_auto_upload config")
    return {"binary": "sau", "platforms": {}, "omniroute": {"use_ai_orchestrator": True}, "safety": {"require_explicit_publish": True}}


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _idem(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _platform_cfg(platform: str) -> dict[str, Any]:
    cfg = load_config()
    return (cfg.get("platforms") or {}).get(platform, {})


def supported_platforms() -> dict[str, Any]:
    cfg = load_config()
    out = {}
    for name, spec in PLATFORM_SPECS.items():
        pc = (cfg.get("platforms") or {}).get(name, {})
        out[name] = {**spec, "enabled": bool(pc.get("enabled", False)), "capabilities": pc.get("capabilities", []), "executor": "sau-cli" if pc.get("enabled") else "unconfigured"}
    return out


async def generate_platform_ad_copy(
    *,
    platform: str,
    product: Mapping[str, Any],
    campaign_goal: str = "conversion",
    language: str = "de",
    extra_instructions: str = "",
) -> dict[str, Any]:
    """Generate platform-adapted ad/social copy via OmniRoute (auto/smart-first)."""
    spec = PLATFORM_SPECS.get(platform, {})
    orch = get_ai_orchestrator()
    facts = {k: product.get(k) for k in [
        "sku", "title", "name", "brand", "category", "sale_price", "price", "currency",
        "short_description", "raw_description", "seo_title", "meta_description", "slug",
        "main_image_url", "gallery_json", "ean", "material", "color", "size",
    ] if product.get(k) not in (None, "", [], {})}
    prompt = (
        "Erstelle performanten, plattformgerechten E-Commerce-Werbe-/Social-Content als strikt valides JSON. "
        "Nutze ausschließlich belegte Produktdaten, erfinde keine Zertifikate/Eigenschaften. "
        f"Plattform: {platform} ({spec.get('kind','')}, Region {spec.get('region','')}). "
        f"Sprache: {language}. Kampagnenziel: {campaign_goal}. "
        "Liefere Keys: headline/title, body/description, hashtags (Array), cta, hooks (Array), "
        "video_script_15s, video_script_30s, image_ideas (Array), compliance_notes (Array), "
        "platform_limits_hint. "
        f"Zusatzhinweise: {extra_instructions or '-'}\n"
        f"Produktdaten: {json.dumps(facts, ensure_ascii=False)[:6000]}"
    )
    try:
        res = await orch.chat_completion(
            messages=[
                {"role": "system", "content": "Du bist Performance-Marketing- und Social-Commerce-Spezialist. Antworte nur mit JSON."},
                {"role": "user", "content": prompt},
            ],
            module="social_advertising",
            combo=load_config().get("omniroute", {}).get("copy_combo", "auto/smart"),
            task="social_ad_copy",
            use_cache=True,
            temperature=0.4,
        )
        content = (res.get("content") or "").strip() if isinstance(res, dict) else ""
        parsed = _extract_json(content)
        if parsed:
            return {"ok": True, "platform": platform, "copy": parsed, "model": res.get("model") if isinstance(res, dict) else None, "privacy_routed": bool(res.get("privacy_routed")) if isinstance(res, dict) else False}
    except Exception as e:
        logger.warning("AI ad copy failed for %s: %s", platform, e)
    # deterministic fallback
    title = str(facts.get("title") or facts.get("name") or facts.get("sku") or "Produkt")
    price = facts.get("sale_price") or facts.get("price")
    brand = facts.get("brand") or ""
    desc = str(facts.get("short_description") or facts.get("raw_description") or "").strip()[:280]
    return {
        "ok": True,
        "platform": platform,
        "fallback": True,
        "copy": {
            "title": f"{title} – jetzt entdecken",
            "description": (desc or f"{brand + ' ' if brand else ''}{title}. {('Preis: ' + str(price) + ' EUR. ') if price else ''}Mehr Infos im Shop."),
            "hashtags": ["#shop", "#angebot", "#neu", f"#{str(facts.get('category') or 'produkte').replace(' ','')[:20]}"],
            "cta": "Jetzt ansehen",
            "hooks": [f"Neu: {title}", "Limitiert verfügbar" if price else "Jetzt entdecken"],
            "video_script_15s": f"Hook: {title}. Nutzen zeigen. CTA: Jetzt im Shop sichern.",
            "video_script_30s": f"Intro {title}. 2-3 Vorteile aus bekannten Daten. Close-up. CTA.",
            "image_ideas": [f"Produktfoto {title}", "Detailaufnahme", "Lifestyle/Anwendung"],
            "compliance_notes": ["Nur bestätigte Produktdaten verwenden", "Rabatte/Verfügbarkeit vor Veröffentlichung prüfen"],
            "platform_limits_hint": spec.get("video_fields") or spec.get("note_fields") or [],
        },
        "model": "fallback-template",
    }


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    t = text.strip()
    if "```" in t:
        for part in t.split("```"):
            part = part.strip()
            if part.lower().startswith("json"):
                part = part[4:].strip()
            if part.startswith("{") and part.endswith("}"):
                t = part
                break
    s, e = t.find("{"), t.rfind("}")
    if s != -1 and e > s:
        try:
            obj = json.loads(t[s:e + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def build_sau_command(
    *,
    platform: str,
    action: str,
    account: str,
    file_path: str | None = None,
    images: list[str] | None = None,
    title: str = "",
    desc: str = "",
    note: str = "",
    tags: list[str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> list[str]:
    """Build `sau <platform> <action> ...` command (no shell interpolation)."""
    cfg = load_config()
    binary = str(cfg.get("binary") or "sau")
    cmd = [binary, platform, action, "--account", account]
    tags = tags or []
    extra = extra or {}
    if action == "upload-video":
        if file_path:
            cmd += ["--file", file_path]
        if title:
            cmd += ["--title", title]
        if desc:
            cmd += ["--desc", desc]
    elif action == "upload-note":
        for img in (images or []):
            cmd += ["--images", img]
        if title:
            cmd += ["--title", title]
        if note:
            cmd += ["--note", note]
    for t in tags[:20]:
        cmd += ["--tag", str(t)]
    # safe allowlist for known extras
    for k in ("tid", "publish_strategy", "headless"):
        if k in extra and extra[k] not in (None, ""):
            v = extra[k]
            flag = f"--{k.replace('_','-')}"
            if isinstance(v, bool):
                if v:
                    cmd.append(flag)
            else:
                cmd += [flag, str(v)]
    return cmd


@dataclass
class PublishPlan:
    id: str
    platform: str
    action: str
    account: str
    command: list[str]
    payload: dict[str, Any]
    idempotency_key: str
    created_at: str
    status: str = "planned"


def plan_publish_job(
    *,
    platform: str,
    action: str,
    account: str,
    payload: Mapping[str, Any],
) -> PublishPlan:
    if platform not in PLATFORM_SPECS:
        raise ValueError(f"unsupported_platform:{platform}")
    file_path = str(payload.get("file") or payload.get("file_path") or "") or None
    images = list(payload.get("images") or [])
    title = str(payload.get("title") or "")
    desc = str(payload.get("desc") or payload.get("description") or "")
    note = str(payload.get("note") or payload.get("body") or desc)
    tags = list(payload.get("tags") or payload.get("hashtags") or [])
    cmd = build_sau_command(platform=platform, action=action, account=account, file_path=file_path, images=images, title=title, desc=desc, note=note, tags=[str(t).lstrip("#") for t in tags], extra=payload)
    idem = _idem(platform, action, account, title, desc, note, ",".join(images), file_path or "")
    return PublishPlan(
        id=_id("spub"),
        platform=platform,
        action=action,
        account=account,
        command=cmd,
        payload=dict(payload),
        idempotency_key=idem,
        created_at=utc_now(),
        status="planned",
    )


async def execute_publish_plan(plan: PublishPlan, *, timeout: int = 600, dry_run: bool = False) -> dict[str, Any]:
    """Execute a planned publish via local `sau` CLI. Returns structured result; never raises secrets."""
    cfg = load_config()
    if dry_run or cfg.get("safety", {}).get("executor_mode") == "dry_run":
        return {"ok": True, "dry_run": True, "plan_id": plan.id, "command": plan.command, "status": "dry_run"}
    binary = str(cfg.get("binary") or "sau")
    if not plan.command or plan.command[0] != binary:
        return {"ok": False, "error": "invalid_command", "plan_id": plan.id}
    workdir = str(cfg.get("workdir") or ".")
    try:
        proc = await asyncio.create_subprocess_exec(
            *plan.command,
            cwd=workdir if Path(workdir).exists() else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "error": "timeout", "plan_id": plan.id, "timeout": timeout}
        stdout = (stdout_b or b"").decode("utf-8", errors="replace")[-8000:]
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")[-4000:]
        ok = proc.returncode == 0
        return {"ok": ok, "plan_id": plan.id, "returncode": proc.returncode, "stdout": stdout, "stderr": stderr, "command": plan.command}
    except FileNotFoundError:
        return {"ok": False, "error": "executor_not_found", "binary": binary, "plan_id": plan.id, "hint": "Install/clone social-auto-upload and expose `sau` in PATH or configure workdir/binary."}
    except Exception as e:
        return {"ok": False, "error": f"exec_failed:{type(e).__name__}", "plan_id": plan.id}
