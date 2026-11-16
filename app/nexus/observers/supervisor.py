"""Observer-Supervisor: monitors agent/skill executions, latency, token/error rates, self-heal restarts."""
from __future__ import annotations
import time, logging
from collections import defaultdict, deque
from typing import Any
from app.nexus.event_bus.store import nexus_event

logger = logging.getLogger(__name__)

class NexusSupervisor:
    def __init__(self) -> None:
        self.executions: deque[dict[str, Any]] = deque(maxlen=500)
        self.errors: deque[dict[str, Any]] = deque(maxlen=200)
        self.restarts: list[dict[str, Any]] = []
        self.agents: dict[str, dict[str, Any]] = {}
        self.started_at = time.time()

    def register_agent(self, name: str, meta: dict[str, Any] | None = None) -> None:
        self.agents[name] = {"name": name, "status": "idle", "last_seen": None, "runs": 0, "errors": 0, "avg_latency_ms": 0, **(meta or {})}

    async def record(self, agent: str, skill: str, ok: bool, latency_ms: int, tokens: int = 0, error: str | None = None, correlation_id: str = "") -> None:
        rec = {"ts": time.time(), "agent": agent, "skill": skill, "ok": ok, "latency_ms": latency_ms, "tokens": tokens, "error": error}
        self.executions.append(rec)
        a = self.agents.setdefault(agent, {"name": agent, "status": "idle", "runs": 0, "errors": 0, "avg_latency_ms": 0})
        a["runs"] += 1; a["last_seen"] = rec["ts"]; a["status"] = "healthy" if ok else "degraded"
        n = a["runs"]; a["avg_latency_ms"] = int(((a.get("avg_latency_ms",0)*(n-1))+latency_ms)/max(n,1))
        if not ok:
            a["errors"] += 1; self.errors.append(rec)
            await nexus_event("AgentExecutionFailed", {"agent": agent, "skill": skill, "error": error, "latency_ms": latency_ms}, source="nexus.supervisor", entity_type="agent", entity_id=agent, correlation_id=correlation_id)
            # self-heal heuristic
            if a["errors"] >= 3 and a["status"] != "restarting":
                await self.self_heal(agent, reason=f"repeated_failures:{error}")
        else:
            await nexus_event("AgentExecutionOk", {"agent": agent, "skill": skill, "latency_ms": latency_ms, "tokens": tokens}, source="nexus.supervisor", entity_type="agent", entity_id=agent, correlation_id=correlation_id)

    async def self_heal(self, agent: str, reason: str = "") -> dict[str, Any]:
        a = self.agents.setdefault(agent, {"name": agent, "runs": 0, "errors": 0})
        a["status"] = "restarting"; a["errors"] = 0
        ev = {"ts": time.time(), "agent": agent, "reason": reason, "action": "restart"}
        self.restarts.append(ev)
        await nexus_event("AgentSelfHealRestart", ev, source="nexus.supervisor", entity_type="agent", entity_id=agent)
        a["status"] = "healthy"
        return {"ok": True, **ev}

    def snapshot(self) -> dict[str, Any]:
        total = len(self.executions); errs = sum(1 for e in self.executions if not e["ok"])
        lat = [e["latency_ms"] for e in self.executions]
        toks = sum(e.get("tokens",0) for e in self.executions)
        return {
            "uptime_s": int(time.time()-self.started_at),
            "agents": list(self.agents.values()),
            "executions_total": total,
            "errors_total": errs,
            "error_rate": round(errs/total,4) if total else 0.0,
            "avg_latency_ms": int(sum(lat)/len(lat)) if lat else 0,
            "token_events": toks,
            "recent_errors": list(self.errors)[-20:],
            "recent_executions": list(self.executions)[-30:],
            "restarts": self.restarts[-20:],
        }

nexus_supervisor = NexusSupervisor()
