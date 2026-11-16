"""API routes for multi-agent orchestration (coding, planning, ops) via OmniRoute gateway."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body

from app.nexus.orchestration.multi_agent import multi_agent_orchestrator

router = APIRouter(prefix="/orchestration", tags=["nexus-orchestration"])


@router.get("/capabilities")
async def orchestration_capabilities() -> dict[str, Any]:
    return multi_agent_orchestrator.capabilities()


@router.post("/run")
async def orchestration_run(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    task = str(payload.get("task") or payload.get("prompt") or "").strip()
    if not task:
        return {"ok": False, "error": "task_required"}
    return await multi_agent_orchestrator.orchestrate(
        task=task,
        mode=str(payload.get("mode") or "pipeline"),
        roles=payload.get("roles"),
        context=str(payload.get("context") or ""),
        model_map=payload.get("model_map") or {},
        role=str(payload.get("role") or "coder"),
        rounds=int(payload.get("rounds") or 1),
        synthesize=bool(payload.get("synthesize", True)),
        include_tests=bool(payload.get("include_tests", False)),
    )
