"""Natural-language Admin & Business Copilot (Control Agent) for Omni-Core.

Security model: LLM never gets direct write access. Read tools execute immediately;
write-capable tools produce a pending action (preview/diff) that requires explicit
confirmation. Confirmed actions are executed server-side with policy checks and
audited. Protected payment/fulfillment invariants cannot be modified.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from database import (
    agent_action_create,
    agent_action_get,
    agent_action_update,
    crm_create_appointment,
    fetch_order,
    get_connection,
    get_pg_pool,
    get_setting,
    list_orders,
    set_setting,
    uses_postgres,
    write_log,
)
from services.omniroute_client import OmniRouteClient


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AgentChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    conversation_id: str | None = Field(default=None, max_length=128)


class ChannelConfigInput(BaseModel):
    channel: Literal["shopware", "amazon", "ebay", "email", "messenger", "google"]
    operation: Literal["set", "remove"]
    key: str = Field(min_length=1, max_length=128)
    value: str = Field(default="", max_length=4096)


class BusinessRuleInput(BaseModel):
    rule_key: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=4096)
    description: str = Field(default="", max_length=1000)


class CrmCalendarInput(BaseModel):
    action: Literal["create_appointment", "create_message_draft"]
    title: str = Field(default="", max_length=200)
    starts_at: str = Field(default="", max_length=64)
    ends_at: str = Field(default="", max_length=64)
    customer_id: str = Field(default="", max_length=128)
    customer_reference: str = Field(default="", max_length=256)
    notes: str = Field(default="", max_length=12000)
    body: str = Field(default="", max_length=12000)


class CreativeToolInput(BaseModel):
    action: Literal[
        "capabilities", "list_models", "list_projects", "list_jobs", "list_assets",
        "list_model_profiles", "seed_model_profiles", "plan_image_generation",
        "plan_product_photography", "plan_creative", "execute_job"
    ]
    capability: str = Field(default="image_generation", max_length=64)
    job_id: str = Field(default="", max_length=128)
    prompt: str = Field(default="", max_length=8000)
    negative_prompt: str = Field(default="", max_length=4000)
    variants: int = Field(default=1, ge=1, le=12)
    product_sku: str = Field(default="", max_length=128)
    model_profile_id: str = Field(default="", max_length=128)
    models: list[str] = Field(default_factory=list)
    scene: str = Field(default="studio", max_length=128)
    shot_type: str = Field(default="product", max_length=128)
    pose: str = Field(default="", max_length=128)
    camera: str = Field(default="", max_length=128)
    lighting: str = Field(default="", max_length=128)
    aspect_ratio: str = Field(default="4:5", max_length=32)
    project_id: str = Field(default="", max_length=128)


TOOL_DEFINITIONS = [
    {"type": "function", "function": {
        "name": "system_diagnostics",
        "description": "Read-only diagnostics for health, orders, payment status, fulfillment locks and tracking.",
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "string", "enum": ["health", "orders", "payment", "tracking", "integrations"]},
            "order_id": {"type": "string"}
        }, "required": ["scope"], "additionalProperties": False}
    }},
    {"type": "function", "function": {
        "name": "manage_channel_config",
        "description": "Prepare an allowed channel configuration change. Never modify security or fulfillment/payment invariants. Requires confirmation.",
        "parameters": {"type": "object", "properties": {
            "channel": {"type": "string", "enum": ["shopware", "amazon", "ebay", "email", "messenger", "google"]},
            "operation": {"type": "string", "enum": ["set", "remove"]},
            "key": {"type": "string"}, "value": {"type": "string"}
        }, "required": ["channel", "operation", "key", "value"], "additionalProperties": False}
    }},
    {"type": "function", "function": {
        "name": "manage_business_rules",
        "description": "Prepare a business rule/autoresponder/notification change. Protected payment/fulfillment rules are immutable. Requires confirmation.",
        "parameters": {"type": "object", "properties": {
            "rule_key": {"type": "string"}, "value": {"type": "string"}, "description": {"type": "string"}
        }, "required": ["rule_key", "value"], "additionalProperties": False}
    }},
    {"type": "function", "function": {
        "name": "crm_and_calendar_actions",
        "description": "Prepare a CRM/calendar action or customer message draft. Requires confirmation.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create_appointment", "create_message_draft"]},
            "title": {"type": "string"}, "starts_at": {"type": "string"}, "ends_at": {"type": "string"},
            "customer_id": {"type": "string"}, "customer_reference": {"type": "string"},
            "notes": {"type": "string"}, "body": {"type": "string"}
        }, "required": ["action"], "additionalProperties": False}
    }},
    {"type": "function", "function": {
        "name": "multimodal_creative",
        "description": "Multimodal creative engine (Tool Registry): read capabilities/routing/models/projects/jobs/assets/profiles or PREPARE creative plans and execution for image, editing, upscaling/removal, video (text/image/video), audio/speech/transcription and multimodal analysis via capability-based routing/fallback/cost hooks. Generation/execution always requires confirmation and uses only configured OmniRoute/provider capabilities; never fabricates outputs.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["capabilities", "list_models", "list_projects", "list_jobs", "list_assets", "list_model_profiles", "seed_model_profiles", "plan_image_generation", "plan_product_photography", "plan_creative", "execute_job"]},
            "capability": {"type": "string", "enum": ["text_generation","image_generation","image_editing","image_analysis","image_upscaling","background_removal","inpainting","outpainting","text_to_video","image_to_video","video_to_video","video_analysis","audio_generation","speech_generation","transcription","multimodal_understanding"]},
            "job_id": {"type": "string"}, "prompt": {"type": "string"}, "negative_prompt": {"type": "string"},
            "variants": {"type": "integer"}, "product_sku": {"type": "string"}, "model_profile_id": {"type": "string"},
            "models": {"type": "array", "items": {"type": "string"}}, "scene": {"type": "string"}, "shot_type": {"type": "string"},
            "pose": {"type": "string"}, "camera": {"type": "string"}, "lighting": {"type": "string"}, "aspect_ratio": {"type": "string"},
            "project_id": {"type": "string"}
        }, "required": ["action"], "additionalProperties": False}
    }},
    {"type": "function", "function": {
        "name": "start_catalog_enrichment",
        "description": "Startet asynchrone KI-Katalog-Optimierung (SEO/Texte/Attribute) für Matterhorn-Produkte ohne/mit fehlenden Beschreibungen. Generierung nur über konfigurierte OmniRoute-Combos. Erfordert Bestätigung.",
        "parameters": {"type": "object", "properties": {
            "only_missing": {"type": "boolean", "description": "Nur Produkte mit fehlenden Texten/SEO optimieren (Standard true)."},
            "limit": {"type": "integer", "description": "Max. Produkte (1-1000)."},
            "generate_images": {"type": "boolean", "description": "Bildgenerierung planen (nur wenn Provider-Fähigkeit verifiziert)."},
            "generate_videos": {"type": "boolean", "description": "Video-Planung (nur wenn Provider-Fähigkeit verifiziert)."},
            "concurrency": {"type": "integer", "description": "Parallele Worker (1-10)."}
        }, "required": [], "additionalProperties": False}
    }},
    {"type": "function", "function": {
        "name": "ai_usage_report",
        "description": "Liest KI-Kosten/Telemetrie/Latenzwerte (heute/gesamt), Budgetstatus und letzte Requests. Read-only.",
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "string", "enum": ["summary", "today", "recent"]},
            "limit": {"type": "integer"}
        }, "required": ["scope"], "additionalProperties": False}
    }},
    {"type": "function", "function": {
        "name": "set_strict_privacy_mode",
        "description": "Aktiviert/deaktiviert strikten lokalen Privacy-Modus (PII/CRM/Bestellungen -> omni-local-gdpr, keine Cloud-Leaks). Erfordert Bestätigung.",
        "parameters": {"type": "object", "properties": {
            "enabled": {"type": "boolean"}
        }, "required": ["enabled"], "additionalProperties": False}
    }},

    {"type": "function", "function": {
        "name": "manual_bundle_builder",
        "description": "Erstellt/aktualisiert ein Produkt-Set/Bundle manuell aus angegebenen Komponenten-SKUs (manuelle Set-Bildung). Erfordert Bestätigung.",
        "parameters": {"type": "object", "properties": {
            "bundle_sku": {"type": "string"},
            "title": {"type": "string"},
            "component_skus": {"type": "array", "items": {"type": "string"}}
        }, "required": ["bundle_sku", "title", "component_skus"], "additionalProperties": False}
    }},
    {"type": "function", "function": {
        "name": "auto_bundle_generation",
        "description": "Erzeugt automatische Bundle-Vorschläge oder erstellt Bundles aus datenbasierten Signalen (Kategorien, Bestand, Nachfrage-Heuristiken). Erfordert Bestätigung.",
        "parameters": {"type": "object", "properties": {
            "max_bundles": {"type": "integer"},
            "create": {"type": "boolean"}
        }, "required": [], "additionalProperties": False}
    }},
    {"type": "function", "function": {
        "name": "generate_marketplace_assortment_plan",
        "description": "Erstellt automatische AI-Artikelauswahl pro Marktplatz (max. 2000) inkl. datenbasierter Nischenanalyse (Trends/Nachfrage/Bestand; interne News-Signale). Erfordert Bestätigung.",
        "parameters": {"type": "object", "properties": {
            "marketplace": {"type": "string"},
            "max_items": {"type": "integer"},
            "use_ai_niche": {"type": "boolean"}
        }, "required": ["marketplace"], "additionalProperties": False}
    }},
    {"type": "function", "function": {
        "name": "submit_ai_training_feedback",
        "description": "Speichert AI-Training/Feedback (Korrekturen, Bewertungen, Beispiele) zur Verbesserung von Prompts/Qualität. Keine Modellgewichte werden verändert. Erfordert Bestätigung.",
        "parameters": {"type": "object", "properties": {
            "context_type": {"type": "string"},
            "feedback_text": {"type": "string"},
            "rating": {"type": "integer"},
            "input_data": {"type": "object"},
            "output_data": {"type": "object"}
        }, "required": ["feedback_text"], "additionalProperties": False}
    }},
]

PROTECTED_RULE_KEYS = {
    "fulfillment.payment_required",
    "fulfillment.unpaid_order_block",
    "fulfillment.payment_status_gate",
    "fulfillment_locked",
}


def _is_protected_key(key: str) -> bool:
    k = (key or "").lower().strip()
    return key in PROTECTED_RULE_KEYS or k in {p.lower() for p in PROTECTED_RULE_KEYS} or k.startswith("fulfillment") or k.startswith("payment_status")


def _masked(value: str) -> str:
    if len(value) <= 4:
        return "****"
    return value[:2] + "****" + value[-2:]


def _loads(v: Any, default: Any):
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except Exception:
            return default
    return default


async def _diagnostics(args: dict[str, Any]) -> dict[str, Any]:
    scope = args.get("scope", "orders")
    order_id = (args.get("order_id") or "").strip()
    if scope == "health":
        return {"scope": "health", "database": "postgresql" if uses_postgres() else "sqlite"}
    if order_id:
        order = await fetch_order(order_id)
        return {"scope": scope, "order": order}
    orders = await list_orders(limit=25)
    if scope == "payment":
        orders = [o for o in orders if str(o.get("payment_status") or "").lower() not in ("paid", "succeeded", "captured")]
    elif scope == "tracking":
        orders = [o for o in orders if not (o.get("tracking_number") or o.get("tracking_url"))]
    slim = []
    for o in orders[:25]:
        slim.append({k: o.get(k) for k in ("order_id", "sku", "quantity", "marketplace", "payment_status", "fulfillment_locked", "status", "tracking_number", "tracking_url", "created_at", "updated_at") if k in o})
    return {"scope": scope, "orders": slim, "count": len(slim)}


async def _creative_read(payload: CreativeToolInput, runtime: Any = None) -> dict[str, Any]:
    from services.creative_foundation import CreativeCapabilityRegistry, CreativeJobService, MediaDAM, ModelIdentityManager
    a = payload.action
    if a == "capabilities":
        return {"action": a, "capabilities": CreativeCapabilityRegistry.from_runtime(runtime), "routing_image": await CreativeCapabilityRegistry.route_with_fallback(runtime, "image_generation")}
    if a == "list_models":
        reg = CreativeCapabilityRegistry.from_runtime(runtime)
        return {"action": a, "models": reg.get("models", [])}
    if a == "list_projects":
        return {"action": a, "projects": await CreativeJobService.list_projects()}
    if a == "list_jobs":
        return {"action": a, "jobs": await CreativeJobService.list_jobs(project_id=payload.project_id or None)}
    if a == "list_assets":
        return {"action": a, "assets": await MediaDAM.list_assets(product_sku=payload.product_sku or None)}
    if a == "list_model_profiles":
        return {"action": a, "profiles": await ModelIdentityManager.list_profiles()}
    if a == "seed_model_profiles":
        # seeding is a state change -> handled as write path
        return {"action": a}
    return {"action": a}


async def _prepare_creative_action(payload: CreativeToolInput, *, actor: str, prompt: str, conversation_id: str, runtime: Any = None) -> dict[str, Any]:
    from services.ai_photoshooting import ImageGenerationAgent, ProductPhotographyAgent
    from services.creative_agents import CreativeOrchestrator
    if payload.action == "seed_model_profiles":
        diff = {"action": "seed_model_profiles", "note": "Seed canonical model identity profiles (incl. Luna/Raven/Nyx) from references."}
        summary = "Model-Profile (kanonisch) seeden/aktualisieren"
        args = payload.model_dump()
    elif payload.action == "plan_image_generation":
        plan = await ImageGenerationAgent.plan(payload.prompt, negative_prompt=payload.negative_prompt, variants=payload.variants, product_sku=payload.product_sku, model_profile_id=payload.model_profile_id, runtime=runtime)
        diff = {"action": payload.action, "executable": plan.get("executable"), "routing": plan.get("routing"), "provider": plan.get("provider"), "variants": payload.variants, "product_sku": payload.product_sku}
        summary = f"Bildgenerierung vorbereiten ({payload.variants} Variante(n))"
        args = {**payload.model_dump(), "_plan": plan}
    elif payload.action == "plan_product_photography":
        plan = await ProductPhotographyAgent.plan(payload.product_sku, models=payload.models, scene=payload.scene, shot_type=payload.shot_type, variants=payload.variants, pose=payload.pose, camera=payload.camera, lighting=payload.lighting, aspect_ratio=payload.aspect_ratio, runtime=runtime)
        diff = {"action": payload.action, "product_sku": payload.product_sku, "scene": payload.scene, "shot_type": payload.shot_type, "models": payload.models, "variants": payload.variants, "routing": plan.get("routing"), "executable": plan.get("executable")}
        summary = f"Produkt-/Model-Fotoshooting vorbereiten: {payload.product_sku or 'Produkt'}"
        args = {**payload.model_dump(), "_plan": plan}
    elif payload.action == "plan_creative":
        plan = await CreativeOrchestrator.plan(payload.capability, runtime=runtime, prompt=payload.prompt, negative_prompt=payload.negative_prompt, variants=payload.variants, product_sku=payload.product_sku, model_profile_id=payload.model_profile_id, models=payload.models, scene=payload.scene, shot_type=payload.shot_type, pose=payload.pose, camera=payload.camera, lighting=payload.lighting, aspect_ratio=payload.aspect_ratio)
        diff = {"action": payload.action, "capability": payload.capability, "agent": plan.get("agent"), "executable": plan.get("executable"), "routing": plan.get("routing"), "estimated_cost": (plan.get("routing") or {}).get("estimated_cost") if isinstance(plan.get("routing"), dict) else None, "variants": payload.variants}
        summary = f"Creative-Plan ({payload.capability}) vorbereiten"
        args = {**payload.model_dump(), "_plan": plan}
    elif payload.action == "execute_job":
        diff = {"action": "execute_job", "job_id": payload.job_id}
        summary = f"Creative Job ausführen: {payload.job_id or '(aus Plan)'}"
        args = payload.model_dump()
    else:
        raise ValueError("Unsupported creative write action")
    action = await agent_action_create({
        "actor": actor, "prompt": prompt, "tool_name": "multimodal_creative", "args_json": args,
        "preview_json": {"summary": summary, "diff": diff, "conversation_id": conversation_id},
        "status": "pending_confirmation", "requires_confirmation": True,
    })
    return {"readOnly": False, "requiresConfirmation": True, "pendingAction": {"action_id": action["id"], "tool_name": "multimodal_creative", "summary": summary, "diff": diff}}


async def execute_tool_call(name: str, args: dict[str, Any], *, actor: str, prompt: str = "", conversation_id: str = "", runtime: Any = None) -> dict[str, Any]:
    """Execute/prepare a tool call. Read-only -> immediate result; writes -> pending action."""
    if name == "system_diagnostics":
        return {"readOnly": True, "result": await _diagnostics(args)}
    if name == "ai_usage_report":
        from database import ai_usage_summary, ai_usage_logs_list, ai_budget_spend_today_month, get_setting
        scope = (args or {}).get("scope", "summary")
        if scope == "recent":
            limit = min(max(int((args or {}).get("limit") or 20), 1), 100)
            logs = await ai_usage_logs_list(limit=limit)
            return {"readOnly": True, "result": {"scope": "recent", "items": logs, "count": len(logs)}}
        summary = await ai_usage_summary()
        spend = await ai_budget_spend_today_month()
        summary["spend"] = spend
        summary["budgets"] = {
            "daily_usd": await get_setting("ai_budget_daily_usd"),
            "monthly_usd": await get_setting("ai_budget_monthly_usd"),
            "strict_privacy_mode": await get_setting("ai_strict_privacy_mode"),
        }
        return {"readOnly": True, "result": {"scope": scope, "summary": summary}}

    if name == "start_catalog_enrichment":
        payload = {
            "only_missing": bool((args or {}).get("only_missing", True)),
            "limit": min(max(int((args or {}).get("limit") or 200), 1), 1000),
            "generate_images": bool((args or {}).get("generate_images", False)),
            "generate_videos": bool((args or {}).get("generate_videos", False)),
            "concurrency": min(max(int((args or {}).get("concurrency") or 5), 1), 10),
        }
        diff = {"action": "start_catalog_enrichment", **payload}
        summary = f"KI-Katalog-Optimierung starten (nur fehlend={payload['only_missing']}, limit={payload['limit']}, parallel={payload['concurrency']})"
        action = await agent_action_create({
            "actor": actor, "prompt": prompt, "tool_name": "start_catalog_enrichment", "args_json": payload,
            "preview_json": {"summary": summary, "diff": diff, "conversation_id": conversation_id},
            "status": "pending_confirmation", "requires_confirmation": True,
        })
        return {"readOnly": False, "requiresConfirmation": True, "pendingAction": {"action_id": action["id"], "tool_name": "start_catalog_enrichment", "summary": summary, "diff": diff}}
    if name == "set_strict_privacy_mode":
        enabled = bool((args or {}).get("enabled", True))
        diff = {"key": "ai_strict_privacy_mode", "from": await get_setting("ai_strict_privacy_mode"), "to": "1" if enabled else "0"}
        summary = ("Strikten lokalen Privacy-Modus AKTIVIEREN" if enabled else "Strikten lokalen Privacy-Modus DEAKTIVIEREN") + " (PII/CRM -> omni-local-gdpr, kein Cloud-Fallback)."
        action = await agent_action_create({
            "actor": actor, "prompt": prompt, "tool_name": "set_strict_privacy_mode", "args_json": {"enabled": enabled},
            "preview_json": {"summary": summary, "diff": diff, "conversation_id": conversation_id},
            "status": "pending_confirmation", "requires_confirmation": True,
        })
        return {"readOnly": False, "requiresConfirmation": True, "pendingAction": {"action_id": action["id"], "tool_name": "set_strict_privacy_mode", "summary": summary, "diff": diff}}
    if name == "manual_bundle_builder":
        bundle_sku = str((args or {}).get("bundle_sku") or "").strip()
        title = str((args or {}).get("title") or "").strip()
        comps = [str(x).strip() for x in ((args or {}).get("component_skus") or []) if str(x).strip()]
        if not bundle_sku or not title or len(comps) < 2:
            raise ValueError("bundle_sku, title und mindestens 2 component_skus erforderlich.")
        diff = {"bundle_sku": bundle_sku, "title": title, "component_skus": comps}
        summary = f"Manuelles Set/Bundle speichern: {bundle_sku} ({len(comps)} Komponenten)"
        action = await agent_action_create({"actor": actor, "prompt": prompt, "tool_name": name, "args_json": {"bundle_sku": bundle_sku, "title": title, "component_skus": comps}, "preview_json": {"summary": summary, "diff": diff, "conversation_id": conversation_id}, "status": "pending_confirmation", "requires_confirmation": True})
        return {"readOnly": False, "requiresConfirmation": True, "pendingAction": {"action_id": action["id"], "tool_name": name, "summary": summary, "diff": diff}}

    if name == "auto_bundle_generation":
        max_bundles = min(max(int((args or {}).get("max_bundles") or 10), 1), 50)
        create = bool((args or {}).get("create", False))
        diff = {"max_bundles": max_bundles, "create": create}
        summary = ("Automatische Bundles erstellen" if create else "Automatische Bundle-Vorschläge erzeugen") + f" (max {max_bundles})"
        action = await agent_action_create({"actor": actor, "prompt": prompt, "tool_name": name, "args_json": {"max_bundles": max_bundles, "create": create}, "preview_json": {"summary": summary, "diff": diff, "conversation_id": conversation_id}, "status": "pending_confirmation", "requires_confirmation": True})
        return {"readOnly": False, "requiresConfirmation": True, "pendingAction": {"action_id": action["id"], "tool_name": name, "summary": summary, "diff": diff}}

    if name == "generate_marketplace_assortment_plan":
        marketplace = str((args or {}).get("marketplace") or "shopware").strip().lower()
        max_items = min(max(int((args or {}).get("max_items") or 2000), 1), 2000)
        use_ai_niche = bool((args or {}).get("use_ai_niche", True))
        diff = {"marketplace": marketplace, "max_items": max_items, "use_ai_niche": use_ai_niche}
        summary = f"Artikelauswahl/Sortiment für Marktplatz '{marketplace}' erzeugen (max {max_items}, Nischenanalyse)"
        action = await agent_action_create({"actor": actor, "prompt": prompt, "tool_name": name, "args_json": {"marketplace": marketplace, "max_items": max_items, "use_ai_niche": use_ai_niche}, "preview_json": {"summary": summary, "diff": diff, "conversation_id": conversation_id}, "status": "pending_confirmation", "requires_confirmation": True})
        return {"readOnly": False, "requiresConfirmation": True, "pendingAction": {"action_id": action["id"], "tool_name": name, "summary": summary, "diff": diff}}

    if name == "submit_ai_training_feedback":
        feedback_text = str((args or {}).get("feedback_text") or "").strip()
        if not feedback_text:
            raise ValueError("feedback_text erforderlich.")
        payload = {
            "context_type": str((args or {}).get("context_type") or "general"),
            "feedback_text": feedback_text[:8000],
            "rating": (int((args or {}).get("rating")) if (args or {}).get("rating") is not None else None),
            "input_data": (args or {}).get("input_data") or {},
            "output_data": (args or {}).get("output_data") or {},
        }
        diff = {"context_type": payload["context_type"], "rating": payload["rating"], "feedback_preview": payload["feedback_text"][:300]}
        summary = "AI-Training/Feedback speichern (Korrektur/Beispiel)"
        action = await agent_action_create({"actor": actor, "prompt": prompt, "tool_name": name, "args_json": payload, "preview_json": {"summary": summary, "diff": diff, "conversation_id": conversation_id}, "status": "pending_confirmation", "requires_confirmation": True})
        return {"readOnly": False, "requiresConfirmation": True, "pendingAction": {"action_id": action["id"], "tool_name": name, "summary": summary, "diff": diff}}

    if name == "manage_channel_config":
        payload = ChannelConfigInput.model_validate(args).model_dump()
        if _is_protected_key(payload["key"]):
            raise ValueError("Protected fulfillment/payment security controls cannot be modified by the AI agent.")
        setting_key = f"channel.{payload['channel']}.{payload['key']}"
        old = await get_setting(setting_key)
        secret = any(x in payload["key"].lower() for x in ("key", "token", "secret", "password", "credential"))
        diff = {"setting_key": setting_key, "operation": payload["operation"], "old_value": _masked(str(old or "")) if (old and secret) else (old or ""), "new_value": "" if payload["operation"] == "remove" else (_masked(payload["value"]) if secret else payload["value"])}
        action = await agent_action_create({"actor": actor, "prompt": prompt, "tool_name": name, "args_json": payload, "preview_json": {"summary": f"{payload['channel']} config {payload['operation']}: {payload['key']}", "diff": diff, "conversation_id": conversation_id}, "status": "pending_confirmation", "requires_confirmation": True})
        return {"readOnly": False, "requiresConfirmation": True, "pendingAction": {"action_id": action["id"], "tool_name": name, "summary": f"{payload['channel']} Konfiguration {payload['operation']}: {payload['key']}", "diff": diff}}

    if name == "manage_business_rules":
        payload = BusinessRuleInput.model_validate(args).model_dump()
        if _is_protected_key(payload["rule_key"]):
            raise ValueError("Die Fulfillment-/Payment-Schutzregel ist unveränderbar (payment_status != paid => fulfillment blocked).")
        setting_key = f"business_rule.{payload['rule_key']}"
        old = await get_setting(setting_key)
        diff = {"setting_key": setting_key, "old_value": old or "", "new_value": payload["value"], "description": payload.get("description", "")}
        action = await agent_action_create({"actor": actor, "prompt": prompt, "tool_name": name, "args_json": payload, "preview_json": {"summary": f"Geschäftsregel ändern: {payload['rule_key']}", "diff": diff, "conversation_id": conversation_id}, "status": "pending_confirmation", "requires_confirmation": True})
        return {"readOnly": False, "requiresConfirmation": True, "pendingAction": {"action_id": action["id"], "tool_name": name, "summary": f"Geschäftsregel ändern: {payload['rule_key']}", "diff": diff}}

    if name == "crm_and_calendar_actions":
        payload = CrmCalendarInput.model_validate(args).model_dump()
        diff = {k: v for k, v in payload.items() if v}
        action = await agent_action_create({"actor": actor, "prompt": prompt, "tool_name": name, "args_json": payload, "preview_json": {"summary": f"CRM/Kalender-Aktion: {payload['action']}", "diff": diff, "conversation_id": conversation_id}, "status": "pending_confirmation", "requires_confirmation": True})
        return {"readOnly": False, "requiresConfirmation": True, "pendingAction": {"action_id": action["id"], "tool_name": name, "summary": f"CRM/Kalender-Aktion: {payload['action']}", "diff": diff}}

    if name == "configure_omniroute_gateway":
        url = str((args or {}).get("omniroute_url") or "").strip()
        api_key = str((args or {}).get("omniroute_api_key") or "").strip()
        model = str((args or {}).get("default_llm_model") or "").strip()
        if not url and not api_key and not model:
            raise ValueError("Mindestens omniroute_url, omniroute_api_key oder default_llm_model angeben.")
        changes = {}
        if url: changes["omniroute_url"] = url
        if api_key: changes["omniroute_api_key"] = _masked(api_key)
        if model: changes["default_llm_model"] = model
        diff = {"changes": changes, "note": "Verbindet OmniRoute-Gateway; bestehende Auto-Kombo/Modell-Standards bleiben erhalten, sofern nicht überschrieben."}
        summary = "OmniRoute-Verbindung konfigurieren (" + ", ".join(changes.keys()) + ")"
        payload = {"omniroute_url": url, "omniroute_api_key": api_key, "default_llm_model": model}
        action = await agent_action_create({"actor": actor, "prompt": prompt, "tool_name": name, "args_json": payload, "preview_json": {"summary": summary, "diff": diff, "conversation_id": conversation_id}, "status": "pending_confirmation", "requires_confirmation": True})
        return {"readOnly": False, "requiresConfirmation": True, "pendingAction": {"action_id": action["id"], "tool_name": name, "summary": summary, "diff": diff}}

    if name == "configure_omniroute_gateway":
        url = str((args or {}).get("omniroute_url") or "").strip()
        api_key = str((args or {}).get("omniroute_api_key") or "").strip()
        model = str((args or {}).get("default_llm_model") or "").strip()
        if not url and not api_key and not model:
            raise ValueError("Mindestens omniroute_url, omniroute_api_key oder default_llm_model angeben.")
        changes = {}
        if url:
            changes["omniroute_url"] = url
        if api_key:
            changes["omniroute_api_key"] = _masked(api_key)
        if model:
            changes["default_llm_model"] = model
        diff = {"changes": changes, "note": "OmniRoute-Verbindung setzen; Auto-Kombo-Defaults (omnicore-*/auto/*) bleiben erhalten, sofern nicht überschrieben."}
        summary = "OmniRoute-Verbindung konfigurieren (" + ", ".join(changes.keys()) + ")"
        payload = {"omniroute_url": url, "omniroute_api_key": api_key, "default_llm_model": model}
        action = await agent_action_create({"actor": actor, "prompt": prompt, "tool_name": name, "args_json": payload, "preview_json": {"summary": summary, "diff": diff, "conversation_id": conversation_id}, "status": "pending_confirmation", "requires_confirmation": True})
        return {"readOnly": False, "requiresConfirmation": True, "pendingAction": {"action_id": action["id"], "tool_name": name, "summary": summary, "diff": diff}}

    if name == "multimodal_creative":
        payload = CreativeToolInput.model_validate(args)
        read_actions = {"capabilities", "list_models", "list_projects", "list_jobs", "list_assets", "list_model_profiles"}
        if payload.action in read_actions:
            return {"readOnly": True, "result": await _creative_read(payload, runtime=runtime)}
        # seed/plan/execute -> require confirmation
        return await _prepare_creative_action(payload, actor=actor, prompt=prompt, conversation_id=conversation_id, runtime=runtime)

    raise ValueError(f"Tool not allowed: {name}")


async def execute_confirmed(action_id: str, *, actor: str, runtime: Any = None) -> dict[str, Any]:
    action = await agent_action_get(action_id)
    if not action:
        raise ValueError("Aktion nicht gefunden.")
    status = str(action.get("status") or "")
    if status not in ("pending_confirmation", "pending"):
        raise ValueError("Aktion bereits verarbeitet oder nicht mehr freigegeben.")
    if action.get("requires_confirmation") in (0, False, "0") and status != "pending_confirmation":
        raise ValueError("Aktion erfordert keine Bestätigung in diesem Zustand.")
    tool_name = str(action.get("tool_name") or "")
    args = _loads(action.get("args_json"), {})
    prompt = str(action.get("prompt") or "")

    result: dict[str, Any]
    try:
        if tool_name == "manage_channel_config":
            payload = ChannelConfigInput.model_validate(args).model_dump()
            if _is_protected_key(payload["key"]):
                raise ValueError("Protected fulfillment/payment security controls cannot be modified.")
            setting_key = f"channel.{payload['channel']}.{payload['key']}"
            secret = any(x in payload["key"].lower() for x in ("key", "token", "secret", "password", "credential"))
            if payload["operation"] == "remove":
                await set_setting(setting_key, "", is_secret=secret)
                result = {"status": "removed", "key": setting_key}
            else:
                await set_setting(setting_key, payload["value"], is_secret=secret)
                result = {"status": "updated", "key": setting_key}
        elif tool_name == "manage_business_rules":
            payload = BusinessRuleInput.model_validate(args).model_dump()
            if _is_protected_key(payload["rule_key"]):
                raise ValueError("Protected fulfillment/payment rule cannot be modified.")
            key = f"business_rule.{payload['rule_key']}"
            await set_setting(key, payload["value"])
            result = {"status": "updated", "rule_key": payload["rule_key"], "key": key}
        elif tool_name == "crm_and_calendar_actions":
            payload = CrmCalendarInput.model_validate(args).model_dump()
            if payload["action"] == "create_appointment":
                appt = await crm_create_appointment({"customer_id": payload.get("customer_id") or "", "title": payload.get("title") or "Termin", "starts_at": payload.get("starts_at") or utc_now(), "ends_at": payload.get("ends_at") or "", "notes": payload.get("notes") or payload.get("body") or "", "status": "scheduled"})
                result = {"status": "created", "appointment": appt}
            else:
                result = {"status": "draft_prepared", "channel_action": "not_executed", "note": "Message draft recorded; external send adapters must execute explicitly when configured.", "draft": {k: payload.get(k) for k in ("customer_reference", "body", "title")}}
        elif tool_name == "manual_bundle_builder":
            from services.bundle_engine import build_manual_bundle
            result = await build_manual_bundle(str(args.get("bundle_sku") or ""), str(args.get("title") or ""), list(args.get("component_skus") or []))
        elif tool_name == "auto_bundle_generation":
            from services.bundle_engine import auto_generate_bundles
            result = await auto_generate_bundles(max_bundles=int(args.get("max_bundles") or 10), create=bool(args.get("create", False)), actor=actor)
        elif tool_name == "generate_marketplace_assortment_plan":
            from services.assortment_engine import generate_marketplace_assortment
            result = await generate_marketplace_assortment(str(args.get("marketplace") or "shopware"), max_items=int(args.get("max_items") or 2000), use_ai_niche=bool(args.get("use_ai_niche", True)))
            # slim items in action result
            if isinstance(result.get("items"), list):
                result = {**result, "items_preview": result["items"][:30], "items_count": len(result["items"])}
                result.pop("items", None)
        elif tool_name == "submit_ai_training_feedback":
            from database import ai_training_feedback_create
            fb = await ai_training_feedback_create(source="copilot", context_type=str(args.get("context_type") or "general"), input_data=args.get("input_data") or {}, output_data=args.get("output_data") or {}, feedback_text=str(args.get("feedback_text") or ""), rating=(int(args.get("rating")) if args.get("rating") is not None else None), metadata={"conversation_id": None}, created_by=actor)
            result = {"status": "saved", "feedback_id": fb.get("id"), "note": "Feedback gespeichert (kein automatisches Modell-Fine-Tuning)."}

        elif tool_name == "configure_omniroute_gateway":
            p = args or {}
            applied = []
            url = str(p.get("omniroute_url") or "").strip()
            api_key = str(p.get("omniroute_api_key") or "").strip()
            model = str(p.get("default_llm_model") or "").strip()
            if url:
                await set_setting("omniroute_url", url, is_secret=False); applied.append("omniroute_url")
            if api_key:
                await set_setting("omniroute_api_key", api_key, is_secret=True); applied.append("omniroute_api_key")
            if model:
                await set_setting("default_llm_model", model, is_secret=False); applied.append("default_llm_model")
            reconfigured = False
            try:
                from main import configure_omniroute
                await configure_omniroute(); reconfigured = True
            except Exception:
                reconfigured = False
            result = {"status": "configured", "applied": applied, "reconfigured": reconfigured, "note": "OmniRoute-Verbindung gespeichert. Auto-Kombo/Modell-Defaults bleiben aktiv."}
        elif tool_name == "multimodal_creative":
            payload = CreativeToolInput.model_validate(args)
            from services.creative_foundation import ModelIdentityManager, CreativeJobService
            from services.ai_photoshooting import ImageGenerationAgent, ProductPhotographyAgent
            from services.creative_agents import CreativeOrchestrator
            if payload.action == "seed_model_profiles":
                seeded = await ModelIdentityManager.seed_canonical_profiles()
                result = {"status": "seeded", "count": len(seeded), "profiles": [s.get("slug") for s in seeded]}
            elif payload.action == "plan_image_generation":
                plan = args.get("_plan") or await ImageGenerationAgent.plan(payload.prompt, negative_prompt=payload.negative_prompt, variants=payload.variants, product_sku=payload.product_sku, model_profile_id=payload.model_profile_id, runtime=runtime)
                exec_res = await ImageGenerationAgent.execute_plan(plan, actor=actor, project_id=payload.project_id or None, runtime=runtime)
                result = {"status": exec_res.get("status"), "job": exec_res.get("job"), "outputs": exec_res.get("outputs"), "fidelity": exec_res.get("fidelity")}
            elif payload.action == "plan_product_photography":
                plan = args.get("_plan") or await ProductPhotographyAgent.plan(payload.product_sku, models=payload.models, scene=payload.scene, shot_type=payload.shot_type, variants=payload.variants, pose=payload.pose, camera=payload.camera, lighting=payload.lighting, aspect_ratio=payload.aspect_ratio, runtime=runtime)
                exec_res = await ProductPhotographyAgent.execute_plan(plan, actor=actor, project_id=payload.project_id or None, runtime=runtime)
                result = {"status": exec_res.get("status"), "job": exec_res.get("job"), "outputs": exec_res.get("outputs"), "fidelity": exec_res.get("fidelity")}
            elif payload.action == "plan_creative":
                plan = args.get("_plan") or await CreativeOrchestrator.plan(payload.capability, runtime=runtime, prompt=payload.prompt, negative_prompt=payload.negative_prompt, variants=payload.variants, product_sku=payload.product_sku, model_profile_id=payload.model_profile_id, models=payload.models, scene=payload.scene, shot_type=payload.shot_type, pose=payload.pose, camera=payload.camera, lighting=payload.lighting, aspect_ratio=payload.aspect_ratio)
                exec_res = await CreativeOrchestrator.execute(plan, actor=actor, project_id=payload.project_id or None, runtime=runtime)
                result = {"status": exec_res.get("status"), "job": exec_res.get("job"), "outputs": exec_res.get("outputs"), "routing": exec_res.get("routing")}
            elif payload.action == "execute_job":
                job = await CreativeJobService.get_job(payload.job_id) if payload.job_id else None
                result = {"status": "not_executed", "reason": "direct_job_execute_not_supported", "job": job}
            else:
                raise ValueError(f"Unsupported creative action: {payload.action}")
        else:
            raise ValueError(f"Unknown or non-confirmable tool: {tool_name}")
    except Exception as exc:
        await agent_action_update(action_id, status="failed", error_message=str(exc), executed_at=utc_now())
        await write_log("WARN", "ai_agent_action_failed", f"AI agent action failed: {action_id}", {"action_id": action_id, "tool": tool_name, "actor": actor, "error": str(exc)})
        raise

    await agent_action_update(action_id, status="executed", result_json=result, error_message="", executed_at=utc_now())
    try:
        from database import add_audit_log
        await add_audit_log(action="ai_agent.action_executed", entity_type="ai_agent_action", entity_id=action_id, user_id=actor, old_value={"prompt": prompt, "tool": tool_name, "args": args}, new_value=result)
    except Exception:
        pass
    await write_log("INFO", "ai_agent_action_executed", f"AI agent action executed: {action_id}", {"action_id": action_id, "tool": tool_name, "actor": actor})
    return result


async def cancel_action(action_id: str, *, actor: str) -> dict[str, Any]:
    action = await agent_action_get(action_id)
    if not action:
        raise ValueError("Aktion nicht gefunden.")
    if str(action.get("status") or "") in ("executed", "failed", "cancelled"):
        return {"status": str(action.get("status")), "action_id": action_id}
    await agent_action_update(action_id, status="cancelled", executed_at=utc_now(), error_message="cancelled_by_user")
    await write_log("INFO", "ai_agent_action_cancelled", f"AI agent action cancelled: {action_id}", {"action_id": action_id, "actor": actor})
    return {"status": "cancelled", "action_id": action_id}


async def chat_with_agent(message: str, conversation_id: str | None, *, actor: str, runtime: Any = None) -> dict[str, Any]:
    base_url = await get_setting("omniroute_url") or await get_setting("omni_route_base_url")
    api_key = await get_setting("omniroute_api_key") or await get_setting("omni_route_api_key")
    if not base_url:
        raise RuntimeError("OmniRoute ist nicht konfiguriert (omniroute_url/omni_route_base_url fehlt).")
    client = OmniRouteClient(base_url, api_key or "")
    model = await get_setting("omniroute_agent_model") or await get_setting("default_llm_model") or "auto"
    system = (
        "Du bist der sichere Omni-Core Admin & Business Copilot (Control Agent) inkl. Multimodal Creative Engine. "
        "Nutze ausschließlich die bereitgestellten Tools. Lies Daten zuerst, bevor du Änderungen oder Generierungen vorbereitest. "
        "Schreibende/generierende Tools erzeugen IMMER eine Bestätigungsvorschau (requiresConfirmation) und führen nichts direkt aus. "
        "Für die OmniRoute-/KI-Gateway-Verbindung nutze 'configure_omniroute_gateway' (URL und/oder API-Key) und bereite die Änderung zur Bestätigung vor; gib niemals Schlüssel/Secrets im Klartext aus. "
        "Empfohlene Standards sind bereits vorkonfiguriert (auto/smart, omnicore-Combos für Copilot/Content/Coding/Fast/Vision/Fotoshooting inkl. Modell-Identitäten wie Luna/Raven/Nyx). "
        "Führe keine Generation aus und behaupte keine Ergebnisse, wenn Provider/Model/Capabilities nicht konfiguriert sind. "
        "Die Fulfillment-Sperre für unbezahlte Bestellungen ist unveränderbar: payment_status != 'paid' => fulfillment blocked. "
        "Keine Shell-, SQL-, Datei-, Netzwerk- oder beliebige Code-Ausführung. Keine Geheimnisse im Chat ausgeben. "
        "Antworte präzise auf Deutsch und fasse geplante Änderungen klar zusammen."
    )
    conversation_id = conversation_id or str(uuid.uuid4())
    messages = [{"role": "system", "content": system}, {"role": "user", "content": message}]
    pending: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []

    for _ in range(4):
        result = await client.chat_completion(model=model, messages=messages, tools=TOOL_DEFINITIONS, tool_choice="auto")
        calls = result.get("tool_calls", []) or []
        if not calls:
            return {"conversation_id": conversation_id, "message": result.get("content") or "Aktion vorbereitet.", "pending_actions": pending, "tool_results": tool_results, "requiresConfirmation": bool(pending)}
        assistant_message = {"role": "assistant", "content": result.get("content") or "", "tool_calls": [{"id": c.get("id"), "type": "function", "function": {"name": c.get("name"), "arguments": c.get("arguments", "{}")}} for c in calls]}
        messages.append(assistant_message)
        stop_for_confirmation = False
        for call in calls:
            name = str(call.get("name") or "")
            raw_args = call.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except Exception:
                args = {}
            execution = await execute_tool_call(name, args, actor=actor, prompt=message, conversation_id=conversation_id, runtime=runtime)
            tool_results.append({"tool": name, "result": execution})
            if execution.get("pendingAction"):
                pending.append(execution["pendingAction"])
                stop_for_confirmation = True
            messages.append({"role": "tool", "tool_call_id": call.get("id") or str(uuid.uuid4()), "content": json.dumps(execution, ensure_ascii=False, default=str)})
        if stop_for_confirmation:
            return {"conversation_id": conversation_id, "message": result.get("content") or "Änderung vorbereitet. Bitte prüfen und bestätigen.", "pending_actions": pending, "tool_results": tool_results, "requiresConfirmation": True}
    return {"conversation_id": conversation_id, "message": "Maximale Tool-Ausführungstiefe erreicht.", "pending_actions": pending, "tool_results": tool_results, "requiresConfirmation": bool(pending)}
