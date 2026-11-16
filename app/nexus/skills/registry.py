"""Dynamic Nexus Skill Registry - modular capabilities for agents."""
from __future__ import annotations
import time, logging
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

@dataclass
class SkillResult:
    ok: bool
    skill: str
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    latency_ms: int = 0

class NexusSkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {}
        self._meta: dict[str, dict[str, Any]] = {}

    def register(self, name: str, handler: Callable[..., Awaitable[dict[str, Any]]], meta: dict[str, Any] | None = None) -> None:
        self._skills[name] = handler
        self._meta[name] = meta or {}

    def get(self, name: str):
        return self._skills.get(name)

    def list(self) -> list[dict[str, Any]]:
        return [{"name": n, **self._meta.get(n, {})} for n in sorted(self._skills)]

    async def execute(self, name: str, **kwargs: Any) -> SkillResult:
        fn = self._skills.get(name)
        if not fn:
            return SkillResult(ok=False, skill=name, error=f"unknown_skill:{name}")
        t0 = time.monotonic()
        try:
            out = await fn(**kwargs)
            return SkillResult(ok=bool(out.get("ok", True)), skill=name, output=out, latency_ms=int((time.monotonic()-t0)*1000))
        except Exception as e:
            logger.exception("Skill %s failed", name)
            return SkillResult(ok=False, skill=name, error=f"{type(e).__name__}:{e}", latency_ms=int((time.monotonic()-t0)*1000))

nexus_registry = NexusSkillRegistry()
