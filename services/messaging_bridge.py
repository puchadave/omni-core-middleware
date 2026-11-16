"""Messaging & chat channel bridges (WhatsApp via wacli, admin/shop chat adapters).

Adapts:
- wacli (WhatsApp CLI) for CRM omnichannel messaging (send/receive sync hooks)
- chat-js concepts for embeddable admin/shop chat UIs backed by Omni-CORE AI agent
  and OmniRoute (OpenAI-compatible), with strict server-side policy/PII guards.

Executors are external/CLI-based and optional; functions return structured status
when unavailable instead of failing hard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from app.services.ai_orchestrator import get_ai_orchestrator

logger = logging.getLogger(__name__)

CFG_PATH = Path(__file__).resolve().parents[1] / "config" / "integrations" / "messaging_channels.json"


def load_messaging_config() -> dict[str, Any]:
    if CFG_PATH.exists():
        try:
            return json.loads(CFG_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to read messaging config")
    return {}


async def _run_cli(cmd: list[str], *, timeout: int = 60, cwd: str | None = None) -> dict[str, Any]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd,
            env={**os.environ},
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = (out_b or b"").decode("utf-8", errors="replace")
        err = (err_b or b"").decode("utf-8", errors="replace")
        parsed: Any = None
        t = out.strip()
        if t.startswith("{") or t.startswith("["):
            try:
                parsed = json.loads(t)
            except Exception:
                parsed = None
        return {"ok": proc.returncode == 0, "code": proc.returncode, "stdout": out[-12000:], "stderr": err[-4000:], "json": parsed, "command": cmd}
    except FileNotFoundError:
        return {"ok": False, "error": "binary_not_found", "command": cmd}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout", "command": cmd}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}:{e}", "command": cmd}


class WacliBridge:
    """WhatsApp CLI bridge (wacli)."""

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        cfg = load_messaging_config().get("whatsapp", {})
        self.cfg = {**cfg, **(config or {})}
        self.binary = str(self.cfg.get("binary") or "wacli")
        self.store_dir = str(self.cfg.get("store_dir") or "")

    def _base(self) -> list[str]:
        cmd = [self.binary]
        if self.store_dir:
            cmd += ["--store", self.store_dir]
        cmd += ["--json"]
        return cmd

    async def status(self) -> dict[str, Any]:
        # best-effort: auth status + doctor info
        res = await _run_cli(self._base() + ["auth", "status"], timeout=20)
        return {"enabled": bool(self.cfg.get("enabled")), "binary": self.binary, "store_dir": self.store_dir, "auth": res}

    async def send_text(self, to: str, message: str, reply_to: str | None = None) -> dict[str, Any]:
        cmd = self._base() + ["send", "text", "--to", to, "--message", message]
        if reply_to:
            cmd += ["--reply-to", reply_to]
        return await _run_cli(cmd, timeout=60)

    async def send_file(self, to: str, file_path: str, caption: str = "") -> dict[str, Any]:
        cmd = self._base() + ["send", "file", "--to", to, "--file", file_path]
        if caption:
            cmd += ["--caption", caption]
        return await _run_cli(cmd, timeout=120)

    async def list_messages(self, chat: str | None = None, limit: int = 20) -> dict[str, Any]:
        cmd = self._base() + ["messages", "list", "--limit", str(max(1, min(limit, 200)))]
        if chat:
            cmd += ["--chat", chat]
        return await _run_cli(cmd, timeout=30)

    async def search_messages(self, query: str, limit: int = 20) -> dict[str, Any]:
        return await _run_cli(self._base() + ["messages", "search", query, "--limit", str(max(1, min(limit, 100)))], timeout=30)


async def admin_chat_reply(
    *,
    messages: list[dict[str, Any]],
    mode: str = "copilot",
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Server-side admin chat completion routed via OmniRoute (auto/smart-first)."""
    orch = get_ai_orchestrator()
    sys = (
        "Du bist der OMNI-CORE Admin-Copilot. Unterstütze bei E-Commerce, Katalog, Marketing, "
        "SEO, CRM, Pricing-Analyse (assistiv), Social-Publishing-Planung, Creative/Foto-Jobs und OmniRoute-Betrieb. "
        "Keine erfundenen Fakten/Zugangsdaten. Schreibaktionen nur als Vorschlag/Plan; Ausführung erfordert Freigabe. "
        "Respektiere Datenschutz: PII nicht an Cloud leaken (Orchestrator erzwingt Routing)."
    )
    ctx = dict(context or {})
    if ctx:
        sys += f"\nKontext: {json.dumps(ctx, ensure_ascii=False)[:4000]}"
    combo = "auto/smart"
    if mode in {"marketing", "seo"}:
        combo = "omni-ecom-marketing-smart"
    elif mode in {"support", "crm"}:
        combo = "omni-ecom-crm-support-smart"
    elif mode in {"coding", "mapping"}:
        combo = "omni-ecom-coding-smart"
    try:
        res = await orch.chat_completion(
            messages=[{"role": "system", "content": sys}, *messages[-30:]],
            module="admin_chat",
            combo=combo,
            task=f"admin_chat_{mode}",
            use_cache=False,
            temperature=0.2,
        )
        return {"ok": bool(res.get("ok", True)), "reply": res.get("content") or "", "model": res.get("model"), "privacy_routed": bool(res.get("privacy_routed")), "combo": combo}
    except Exception as e:
        logger.exception("admin_chat_reply failed")
        return {"ok": False, "error": str(e), "combo": combo}


async def storefront_chat_reply(
    *,
    messages: list[dict[str, Any]],
    shop_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Customer-facing shop chat (guarded, no internal/admin leakage)."""
    orch = get_ai_orchestrator()
    sys = (
        "Du bist ein hilfreicher Shop-Assistent. Beantworte nur kundenbezogene Fragen zu Produkten, "
        "Verfügbarkeit (wenn bekannt), Versand/Rückgabe (allgemein, falls nicht anders angegeben) und "
        "leite bei Bestell-/Zahlungsproblemen an Support weiter. Keine internen Daten, keine Admin-Infos, "
        "keine Preise/Bestände erfinden, keine Zusagen ohne Bestätigung. Bei sensiblen Daten diskret bleiben."
    )
    if shop_context:
        safe_ctx = {k: v for k, v in dict(shop_context).items() if k in {"shop_name", "locale", "currency", "support_email", "public_catalog_hint"}}
        if safe_ctx:
            sys += f"\nShop-Kontext: {json.dumps(safe_ctx, ensure_ascii=False)[:1500]}"
    try:
        res = await orch.chat_completion(
            messages=[{"role": "system", "content": sys}, *messages[-20:]],
            module="storefront_chat",
            combo="omni-ecom-crm-support-smart",
            task="storefront_support",
            use_cache=False,
            temperature=0.3,
        )
        return {"ok": bool(res.get("ok", True)), "reply": res.get("content") or "", "model": res.get("model"), "privacy_routed": bool(res.get("privacy_routed"))}
    except Exception as e:
        logger.exception("storefront_chat_reply failed")
        return {"ok": False, "error": str(e)}
