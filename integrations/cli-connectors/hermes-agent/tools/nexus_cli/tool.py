"""Hermes Agent tool wrapper for NEXUS/OmniRoute middleware APIs.

Drop into a Hermes-compatible tools directory (e.g. ~/.hermes/tools/nexus_cli/)
and ensure OMNICORE_BASE_URL / OMNICORE_API_KEY are set in the environment.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any

SCRIPT = os.environ.get("NEXUS_CLI_SCRIPT") or os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../scripts/nexus-cli.sh"))


def _run(args: list[str]) -> dict[str, Any]:
    env = os.environ.copy()
    try:
        p = subprocess.run([SCRIPT] + args, capture_output=True, text=True, env=env, timeout=120)
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        try:
            data = json.loads(out) if out else None
        except Exception:
            data = {"raw": out}
        return {"ok": p.returncode == 0, "exit_code": p.returncode, "data": data, "stderr": err}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}:{e}"}


def nexus_status() -> dict[str, Any]:
    """Get NEXUS middleware status."""
    return _run(["nexus-status"])


def nexus_pve_plan(requirements: str, target: str = "lxc", hostname: str = "") -> dict[str, Any]:
    """Generate Proxmox server plan via NEXUS (OmniRoute)."""
    return _run(["pve-plan", requirements, target, hostname])


def nexus_orchestrate(mode: str, task: str, context: str = "") -> dict[str, Any]:
    """Run multi-agent orchestration (single|pipeline|parallel|debate|code|ops)."""
    return _run(["orchestrate", mode, task, context])
