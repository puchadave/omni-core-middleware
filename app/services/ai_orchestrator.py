"""Central AI orchestration layer for OMNI-CORE (policy, privacy, routing, cache, telemetry).

All AI calls in middleware should go through AIOrchestrator to enforce:
- server-side privacy/PII guard (PII -> force omni-local-gdpr, no cloud leak)
- deterministic response cache (hash-based, TTL)
- circuit-breaker + retry/backoff + safe fallback
- FinOps telemetry (ai_usage_logs/ai_usage_records) and budget enforcement
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import httpx

from database import (
    ai_budget_spend_today_month,
    ai_cache_get,
    ai_cache_set,
    ai_usage_log_insert,
    get_setting,
    write_log,
)

logger = logging.getLogger(__name__)

COMBO_ADMIN = "omni-admin-agent"
COMBO_CONTENT = "omni-content-seo"
COMBO_VISION = "omni-vision-photo"
COMBO_VIDEO = "omni-video-gen"
COMBO_CRM = "omni-crm-support"
COMBO_LOCAL_GDPR = "omni-local-gdpr"

DEFAULT_BASE_URL = "http://127.0.0.1:20128/v1"
DEFAULT_FALLBACK_CHAIN = ["auto/smart", "auto", "auto/cheap"]

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_RE = re.compile(r"(?:\+?\d[\d\s()./\-]{7,}\d)")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", re.I)
_ORDER_ID_RE = re.compile(r"\b(?:order|bestell|auftrag|order[_-]?id|#)\s*[:#]?\s*[A-Z0-9-]{4,}\b", re.I)
_ADDRESS_HINT_RE = re.compile(r"\b(str\.?|straße|strasse|weg|platz|allee|gasse|hausnr|postleitzahl|\bplz\b|\d{5}\s+[A-ZÄÖÜ])", re.I)
_NAME_HINT_RE = re.compile(r"\b(kunde|customer|vorname|nachname|empfänger|recipient|herr|frau)\b", re.I)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def detect_pii(text: str) -> dict[str, Any]:
    """Heuristic PII detection (regex + cues). Returns flags/matches summary, never raw values in logs."""
    t = text or ""
    kinds: list[str] = []
    if _EMAIL_RE.search(t):
        kinds.append("email")
    if _PHONE_RE.search(t):
        kinds.append("phone")
    if _IBAN_RE.search(t):
        kinds.append("iban")
    if _ORDER_ID_RE.search(t):
        kinds.append("order_id")
    if _ADDRESS_HINT_RE.search(t):
        kinds.append("address")
    if _NAME_HINT_RE.search(t) and (len(t.split()) >= 2):
        kinds.append("name_hint")
    # customer/order/payment context cues
    if re.search(r"\b(kundennummer|customer[_\s]?id|zahlungsdaten|payment|iban|bic|shipping address|lieferadresse)\b", t, re.I):
        kinds.append("sensitive_context")
    kinds = sorted(set(kinds))
    return {"pii": bool(kinds), "kinds": kinds}


@dataclass
class RouteDecision:
    requested_model: str
    effective_model: str
    privacy_routed: bool
    reason: str
    pii_kinds: list[str]


class AIOrchestrator:
    """Central async AI gateway for OMNI-CORE."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._circuit: dict[str, dict[str, Any]] = {}
        self._circuit_threshold = 4
        self._circuit_cooldown_s = 45
        self._default_timeout = 45.0

    async def start(self) -> None:
        async with self._lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    timeout=httpx.Timeout(self._default_timeout, connect=5.0),
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                )

    async def close(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            await self.start()
        assert self._client is not None
        return self._client

    async def _base_config(self) -> tuple[str, str]:
        base = (
            await get_setting("omniroute_url")
            or await get_setting("omni_route_base_url")
            or os.getenv("OMNIROUTE_BASE_URL")
            or DEFAULT_BASE_URL
        )
        key = (
            await get_setting("omniroute_api_key")
            or await get_setting("omni_route_api_key")
            or os.getenv("OMNIROUTE_API_KEY")
            or ""
        )
        return base.rstrip("/"), key

    async def _strict_privacy_mode(self) -> bool:
        v = await get_setting("ai_strict_privacy_mode")
        return str(v or "").strip().lower() in {"1", "true", "yes", "on", "strict", "local"}

    async def decide_route(self, *, requested_model: str, prompt_text: str, module: str = "", force_local: bool = False) -> RouteDecision:
        pii = detect_pii(prompt_text)
        strict = force_local or await self._strict_privacy_mode()
        if pii["pii"] or strict:
            reason = "pii_detected" if pii["pii"] else "strict_privacy_mode"
            return RouteDecision(COMBO_LOCAL_GDPR, COMBO_LOCAL_GDPR, True, reason, pii.get("kinds", []))
        return RouteDecision(requested_model, requested_model, False, "normal", [])

    def _circuit_open(self, key: str) -> bool:
        st = self._circuit.get(key)
        if not st:
            return False
        if st.get("open_until", 0) > time.time():
            return True
        if st.get("open_until", 0):
            # cooldown passed, half-open reset
            self._circuit[key] = {"failures": 0, "open_until": 0}
        return False

    def _record_failure(self, key: str) -> None:
        st = self._circuit.setdefault(key, {"failures": 0, "open_until": 0})
        st["failures"] = int(st.get("failures", 0)) + 1
        if st["failures"] >= self._circuit_threshold:
            st["open_until"] = time.time() + self._circuit_cooldown_s

    def _record_success(self, key: str) -> None:
        self._circuit[key] = {"failures": 0, "open_until": 0}

    async def _budget_mode(self) -> str:
        # returns normal|limited|blocked
        try:
            limits = {
                "daily": float(await get_setting("ai_budget_daily_usd") or 0),
                "monthly": float(await get_setting("ai_budget_monthly_usd") or 0),
            }
            if limits["daily"] <= 0 and limits["monthly"] <= 0:
                return "normal"
            spent = await ai_budget_spend_today_month()
            for period, limit in limits.items():
                if limit <= 0:
                    continue
                used = float(spent.get(period, 0.0))
                ratio = used / limit if limit else 0
                if ratio >= 1.0:
                    return "blocked"
                if ratio >= 0.85:
                    return "limited"
            return "normal"
        except Exception:
            return "normal"

    def _fallback_candidates(self, effective_model: str, privacy_routed: bool, budget_mode: str) -> list[str]:
        if privacy_routed or effective_model == COMBO_LOCAL_GDPR:
            # strict: no cloud fallback (best-effort local-only chain)
            return [COMBO_LOCAL_GDPR]
        chain = [effective_model]
        if budget_mode in {"limited", "blocked"}:
            chain += [await_sync_fallback_hint(), "auto/cheap", COMBO_LOCAL_GDPR]
        else:
            chain += list(DEFAULT_FALLBACK_CHAIN)
        # de-dup preserve order
        out: list[str] = []
        for m in chain:
            if not m:
                continue
            if m not in out:
                out.append(m)
        return out

    async def chat_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        module: str = "generic",
        combo: str | None = None,
        model: str | None = None,
        task: str = "chat",
        use_cache: bool = False,
        cache_ttl_seconds: int | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        force_local: bool = False,
        metadata: Mapping[str, Any] | None = None,
        retries: int = 2,
    ) -> dict[str, Any]:
        """OpenAI-compatible chat completion via OmniRoute with guardrails."""
        requested = (model or combo or await get_setting("default_llm_model") or "auto/smart").strip()
        prompt_text = "\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
        decision = await self.decide_route(requested_model=requested, prompt_text=prompt_text, module=module, force_local=force_local)
        budget_mode = await self._budget_mode()

        # cache (deterministic tasks only, never for PII-routed prompts)
        cache_key = None
        if use_cache and not decision.privacy_routed and str(await get_setting("ai_cache_enabled") or "1") != "0":
            ttl = int(cache_ttl_seconds or int(await get_setting("ai_cache_ttl_seconds") or 3600))
            cache_key = _stable_hash({"m": decision.effective_model, "t": task, "temp": temperature, "msgs": messages})
            cached = await ai_cache_get(cache_key)
            if cached is not None:
                await self._log_usage(module=module, combo=decision.effective_model, model=decision.effective_model, latency_ms=0,
                                      prompt_tokens=0, completion_tokens=0, status="cache_hit", privacy_routed=decision.privacy_routed,
                                      metadata={"task": task, "cache": True, "pii_kinds": decision.pii_kinds})
                return {"ok": True, "content": (cached.get("content") if isinstance(cached, dict) else cached),
                        "model": decision.effective_model, "privacy_routed": decision.privacy_routed,
                        "cache_hit": True, "usage": {}, "raw": cached.get("raw") if isinstance(cached, dict) else None}

        # budget hard-stop -> force local/cheap where safe (privacy already forced)
        candidates = self._fallback_candidates(decision.effective_model, decision.privacy_routed, budget_mode)
        if budget_mode == "blocked" and not decision.privacy_routed:
            # prefer cheap/local combo
            for pref in ("auto/cheap", COMBO_LOCAL_GDPR, "auto"):
                if pref not in candidates:
                    candidates.insert(0, pref)

        base_url, api_key = await self._base_config()
        url = f"{base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        last_err: str | None = None
        start = time.monotonic()
        client = await self._get_client()
        for cand in candidates:
            if self._circuit_open(f"{base_url}|{cand}"):
                last_err = f"circuit_open:{cand}"
                continue
            payload: dict[str, Any] = {"model": cand, "messages": messages}
            if temperature is not None:
                payload["temperature"] = temperature
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens
            for attempt in range(retries + 1):
                t0 = time.monotonic()
                try:
                    resp = await client.post(url, headers=headers, json=payload)
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise httpx.HTTPStatusError(f"upstream_{resp.status_code}", request=resp.request, response=resp)
                    resp.raise_for_status()
                    data = resp.json()
                    content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
                    usage = data.get("usage") or {}
                    self._record_success(f"{base_url}|{cand}")
                    total_latency = int((time.monotonic() - start) * 1000)
                    await self._log_usage(module=module, combo=cand, model=data.get("model") or cand, latency_ms=total_latency,
                                          prompt_tokens=int(usage.get("prompt_tokens") or 0),
                                          completion_tokens=int(usage.get("completion_tokens") or 0),
                                          status="success", privacy_routed=decision.privacy_routed,
                                          metadata={"task": task, "requested": requested, "budget_mode": budget_mode, **dict(metadata or {})})
                    if cache_key and use_cache and not decision.privacy_routed:
                        await ai_cache_set(cache_key, {"content": content, "raw": data, "model": cand}, ttl_seconds=int(cache_ttl_seconds or int(await get_setting("ai_cache_ttl_seconds") or 3600)), module=module)
                    return {"ok": True, "content": content, "model": cand, "requested_model": requested,
                            "privacy_routed": decision.privacy_routed, "pii_kinds": decision.pii_kinds,
                            "cache_hit": False, "usage": usage, "raw": data, "fallback_used": cand != decision.effective_model}
                except Exception as e:  # noqa: BLE001
                    last_err = f"{cand}:{type(e).__name__}"
                    # do not retry on auth/invalid request indicators
                    code = getattr(getattr(e, 'response', None), 'status_code', None)
                    if code in (400, 401, 403, 404, 422):
                        self._record_failure(f"{base_url}|{cand}")
                        break
                    if attempt < retries:
                        await asyncio.sleep(min(0.15 * (2 ** attempt) + random.random() * 0.1, 1.2))
                        continue
                    self._record_failure(f"{base_url}|{cand}")
                    break
            # privacy-routed: never try cloud/other fallbacks
            if decision.privacy_routed:
                break

        total_latency = int((time.monotonic() - start) * 1000)
        await self._log_usage(module=module, combo=decision.effective_model, model=decision.effective_model, latency_ms=total_latency,
                              prompt_tokens=0, completion_tokens=0, status="error", privacy_routed=decision.privacy_routed,
                              error_message=str(last_err or "request_failed"),
                              metadata={"task": task, "requested": requested, "budget_mode": budget_mode, **dict(metadata or {})})
        return {"ok": False, "error": last_err or "request_failed", "model": decision.effective_model,
                "privacy_routed": decision.privacy_routed, "cache_hit": False}

    async def _log_usage(self, *, module: str, combo: str, model: str, latency_ms: int, prompt_tokens: int, completion_tokens: int,
                         status: str, privacy_routed: bool, error_message: str = "", metadata: Mapping[str, Any] | None = None) -> None:
        # naive cost estimate (configurable rate), keeps accounting deterministic without provider pricing
        in_rate = float(await get_setting("ai_cost_input_per_1k") or 0.0)
        out_rate = float(await get_setting("ai_cost_output_per_1k") or 0.0)
        est_cost = (prompt_tokens / 1000.0) * in_rate + (completion_tokens / 1000.0) * out_rate
        rec = {
            "id": str(uuid.uuid4()),
            "module": module,
            "combo_used": combo,
            "model": model,
            "latency_ms": int(latency_ms),
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "estimated_cost": float(round(est_cost, 8)),
            "privacy_routed": bool(privacy_routed),
            "status": status,
            "error_message": (error_message or "")[:500],
            "metadata_json": dict(metadata or {}),
            "created_at": _now_iso(),
        }
        try:
            await ai_usage_log_insert(rec)
        except Exception:
            logger.exception("Failed to write ai_usage_log")
        try:
            await write_log("INFO" if status in {"success", "cache_hit"} else "WARN", "ai.request", f"AI {status} {module} {combo}",
                            {"combo": combo, "latency_ms": latency_ms, "privacy_routed": privacy_routed, "status": status})
        except Exception:
            pass


def await_sync_fallback_hint() -> str:
    # helper to pick a conservative cheap model name in sync contexts
    return "omnicore-content-seo"


_orchestrator_singleton: AIOrchestrator | None = None


def get_ai_orchestrator() -> AIOrchestrator:
    global _orchestrator_singleton
    if _orchestrator_singleton is None:
        _orchestrator_singleton = AIOrchestrator()
    return _orchestrator_singleton
