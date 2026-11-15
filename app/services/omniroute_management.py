"""OmniRoute gateway management/discovery client for OMNI-CORE.

Capability-aware: OpenAPI/mgmt endpoints on many OmniRoute deployments require a
management token (not the v1 inference key). This client never invents endpoints or
claims mutations; it probes, reports capabilities, and performs best-effort calls
only when an endpoint responds with a supported schema.

Target (LAN): http://192.168.176.22:20128 (default), also supports public/tunnel URLs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx


DEFAULT_BASE = "http://192.168.176.22:20128"
REQUIRED_COMBOS = [
    "omni-admin-agent",
    "omni-content-seo",
    "omni-vision-photo",
    "omni-video-gen",
    "omni-crm-support",
    "omni-local-gdpr",
]


@dataclass
class ProbeResult:
    ok: bool
    status: int
    path: str
    data: Any = None
    error: str | None = None
    latency_ms: int | None = None


class OmniRouteManagementClient:
    def __init__(self, base_url: str = DEFAULT_BASE, api_key: str = "", mgmt_token: str = "") -> None:
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")
        self.api_v1 = self.base_url if self.base_url.endswith("/v1") else f"{self.base_url}/v1"
        self.api_key = api_key or ""
        self.mgmt_token = mgmt_token or ""
        self._caps: dict[str, Any] = {}

    def _headers_v1(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _headers_mgmt(self) -> dict[str, str]:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        tok = self.mgmt_token or self.api_key
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        return h

    async def _request(self, method: str, url: str, *, headers: dict[str, str] | None = None, json_body: Any = None, timeout: float = 8.0) -> ProbeResult:
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.request(method, url, headers=headers or {}, json=json_body)
            ms = int((time.monotonic() - t0) * 1000)
            data: Any = None
            ct = (r.headers.get("content-type") or "").lower()
            if "json" in ct:
                try:
                    data = r.json()
                except Exception:
                    data = r.text[:2000]
            else:
                # avoid returning huge html blobs
                data = r.text[:1000] if r.text else None
            ok = 200 <= r.status_code < 300
            err = None if ok else (data.get("error", {}).get("message") if isinstance(data, dict) and isinstance(data.get("error"), dict) else str(data)[:200] if data else f"HTTP {r.status_code}")
            return ProbeResult(ok=ok, status=r.status_code, path=url, data=data, error=err, latency_ms=ms)
        except Exception as e:  # noqa: BLE001
            ms = int((time.monotonic() - t0) * 1000)
            return ProbeResult(ok=False, status=0, path=url, data=None, error=f"{type(e).__name__}: {e}", latency_ms=ms)

    async def discover(self) -> dict[str, Any]:
        """Probe reachable endpoints and infer capabilities without mutating state."""
        base = self.base_url.rstrip("/")
        v1 = self.api_v1.rstrip("/")
        probes = {}
        probes["root"] = await self._request("GET", f"{base}/")
        probes["api_health"] = await self._request("GET", f"{base}/api/health")
        probes["v1_models"] = await self._request("GET", f"{v1}/models", headers=self._headers_v1())
        # mgmt probes (best-effort, may 401/403/404)
        for name, path in [
            ("openapi_spec", "/api/openapi/spec"),
            ("docs_json", "/docs/json"),
            ("api_combos", "/api/combos"),
            ("api_providers", "/api/providers"),
            ("api_config", "/api/config"),
            ("api_settings", "/api/settings"),
            ("api_usage", "/api/usage"),
            ("api_metrics", "/api/metrics"),
            ("api_health_db", "/api/health/db"),
        ]:
            probes[name] = await self._request("GET", f"{base}{path}", headers=self._headers_mgmt())
        models = []
        model_ids: list[str] = []
        md = probes["v1_models"].data
        if isinstance(md, dict):
            raw = md.get("data") or []
            if isinstance(raw, list):
                for m in raw:
                    if isinstance(m, dict) and m.get("id"):
                        models.append(m)
                        model_ids.append(str(m["id"]))
        missing = [c for c in REQUIRED_COMBOS if c not in set(model_ids)]
        caps = {
            "base_url": base,
            "api_v1": v1,
            "reachable": probes["api_health"].ok or probes["root"].status in (200, 301, 302, 307),
            "v1_models_ok": probes["v1_models"].ok,
            "model_count": len(model_ids),
            "has_auto_smart": "auto/smart" in set(model_ids),
            "required_combos_present": [c for c in REQUIRED_COMBOS if c in set(model_ids)],
            "required_combos_missing": missing,
            "mgmt_authorized": any(probes[k].ok for k in ("openapi_spec", "api_combos", "api_providers", "api_config", "api_settings")),
            "mgmt_status": {k: {"status": probes[k].status, "ok": probes[k].ok, "error": probes[k].error} for k in ("openapi_spec", "docs_json", "api_combos", "api_providers", "api_config", "api_settings", "api_usage", "api_metrics")},
            "endpoints": {k: {"status": v.status, "ok": v.ok, "latency_ms": v.latency_ms} for k, v in probes.items()},
        }
        self._caps = caps
        return caps

    async def list_models(self) -> list[dict[str, Any]]:
        v1 = self.api_v1.rstrip("/")
        res = await self._request("GET", f"{v1}/models", headers=self._headers_v1())
        if not res.ok or not isinstance(res.data, dict):
            return []
        raw = res.data.get("data") or []
        return [m for m in raw if isinstance(m, dict)] if isinstance(raw, list) else []

    async def list_combos(self) -> ProbeResult:
        return await self._request("GET", f"{self.base_url}/api/combos", headers=self._headers_mgmt())

    async def upsert_combo(self, combo: dict[str, Any]) -> ProbeResult:
        # best-effort: schema differs by OmniRoute version; try POST /api/combos
        return await self._request("POST", f"{self.base_url}/api/combos", headers=self._headers_mgmt(), json_body=combo)

    async def list_providers(self) -> ProbeResult:
        return await self._request("GET", f"{self.base_url}/api/providers", headers=self._headers_mgmt())

    async def get_health(self) -> ProbeResult:
        return await self._request("GET", f"{self.base_url}/api/health")

    async def get_usage(self) -> ProbeResult:
        # try common paths
        for path in ("/api/usage", "/api/metrics", "/api/v1/usage", "/metrics"):
            res = await self._request("GET", f"{self.base_url}{path}", headers=self._headers_mgmt())
            if res.ok:
                res.path = path
                return res
        return await self._request("GET", f"{self.base_url}/api/usage", headers=self._headers_mgmt())

    async def benchmark(self, model: str, prompt: str = "Antworte kurz mit: OK", timeout: float = 20.0) -> dict[str, Any]:
        """Run a minimal chat completion to measure latency/status (uses v1 key)."""
        url = f"{self.api_v1.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json", **self._headers_v1()}
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 16, "temperature": 0}
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(url, headers=headers, json=body)
            ms = int((time.monotonic() - t0) * 1000)
            ok = r.status_code == 200
            data = r.json() if "json" in (r.headers.get("content-type") or "") else {}
            content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") if isinstance(data, dict) else None
            usage = data.get("usage") if isinstance(data, dict) else None
            err = None
            if not ok:
                err = (data.get("error", {}).get("message") if isinstance(data, dict) and isinstance(data.get("error"), dict) else r.text[:200])
            return {"model": model, "ok": ok, "status": r.status_code, "latency_ms": ms, "content_preview": (content or "")[:120] if content else None, "usage": usage, "error": err}
        except Exception as e:  # noqa: BLE001
            ms = int((time.monotonic() - t0) * 1000)
            return {"model": model, "ok": False, "status": 0, "latency_ms": ms, "error": f"{type(e).__name__}: {e}"}
