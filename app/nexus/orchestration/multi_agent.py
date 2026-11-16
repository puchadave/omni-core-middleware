"""Multi-agent orchestration over OmniRoute gateway.

Provides specialized agent roles (planner, coder, reviewer, researcher, ops, tester)
and orchestration modes (pipeline, parallel, debate, code) using AIOrchestrator so
privacy/PII routing, fallbacks, caching and telemetry remain centralized.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

ROLE_DEFAULTS: dict[str, dict[str, Any]] = {
    "planner": {"combo": "omni-admin-agent", "model": "auto/smart", "system": "You are a senior software architect/planner. Produce clear, actionable plans, task breakdowns, risks, and acceptance criteria. Prefer concise structured output."},
    "coder": {"combo": "omni-ecom-coding-smart", "model": "auto/coding", "system": "You are an expert software engineer. Write correct, production-ready, secure code and minimal explanations. Follow existing project conventions. Output code in fenced blocks with file paths when relevant."},
    "reviewer": {"combo": "omni-admin-agent", "model": "auto/smart", "system": "You are a strict code reviewer. Find bugs, security issues, regressions, missing edge cases, and style/convention violations. Provide concrete fixes."},
    "tester": {"combo": "omni-ecom-coding-smart", "model": "auto/coding", "system": "You are a QA/test engineer. Propose and/or write focused tests, verification steps, and edge cases. Prefer runnable commands/tests."},
    "researcher": {"combo": "omni-content-seo", "model": "auto/smart", "system": "You are a technical researcher. Gather relevant facts, compare options, and summarize findings with citations/assumptions clearly marked."},
    "ops": {"combo": "omni-admin-agent", "model": "auto/smart", "system": "You are a DevOps/SRE engineer for Proxmox, Alpine Linux, OpenRC, networking and deployment. Provide safe, idempotent, production-ready steps and commands."},
    "fixer": {"combo": "omni-ecom-coding-smart", "model": "auto/coding", "system": "You are a debugging/fixing agent. Given errors, logs and code, identify root cause and provide the minimal correct fix."},
    "synthesizer": {"combo": "omni-admin-agent", "model": "auto/smart", "system": "You synthesize multiple agent outputs into one coherent, high-quality final answer. Resolve conflicts, remove redundancy, and highlight open issues."},
}

MODE_DESCRIPTIONS = {
    "single": "Run one selected agent role.",
    "pipeline": "Sequential multi-agent pipeline; each stage consumes prior outputs.",
    "parallel": "Run multiple agents in parallel on same task, then synthesize.",
    "debate": "Proposer/critic rounds then synthesis for higher-quality decisions.",
    "code": "Coding pipeline: planner -> coder -> reviewer -> fixer/synthesizer.",
    "ops": "Ops pipeline: planner -> ops executor plan -> reviewer/risk check -> final runbook.",
}


@dataclass
class AgentResult:
    role: str
    ok: bool
    content: str
    model: str | None = None
    combo: str | None = None
    latency_ms: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class MultiAgentOrchestrator:
    def __init__(self) -> None:
        self.gateway_base = (
            os.getenv("OMNIROUTE_URL")
            or os.getenv("OMNI_ROUTE_BASE_URL")
            or "http://192.168.176.22:20128"
        ).rstrip("/")

    def capabilities(self) -> dict[str, Any]:
        return {
            "ok": True,
            "gateway": self.gateway_base,
            "roles": {k: {"default_combo": v.get("combo"), "default_model": v.get("model")} for k, v in ROLE_DEFAULTS.items()},
            "modes": MODE_DESCRIPTIONS,
        }

    async def _chat(self, *, role: str, prompt: str, context: str = "", model: str | None = None, combo: str | None = None, task: str = "copilot_admin", temperature: float | None = None, max_tokens: int | None = None) -> AgentResult:
        t0 = time.monotonic()
        cfg = ROLE_DEFAULTS.get(role, ROLE_DEFAULTS["planner"])
        sysmsg = str(cfg.get("system") or "")
        req_model = model or combo or cfg.get("combo") or cfg.get("model") or "auto/smart"
        messages = [{"role": "system", "content": sysmsg}]
        if context:
            messages.append({"role": "user", "content": f"Context:\n{context}"})
        messages.append({"role": "user", "content": prompt})
        try:
            from app.services.ai_orchestrator import get_ai_orchestrator
            orch = get_ai_orchestrator()
            resp = await orch.chat_completion(
                messages=messages,
                module=f"nexus.multi_agent.{role}",
                combo=req_model,
                model=req_model,
                task=task,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            latency = int((time.monotonic() - t0) * 1000)
            if not resp.get("ok"):
                return AgentResult(role=role, ok=False, content="", model=req_model, combo=req_model, latency_ms=latency, error=str(resp.get("error") or "request_failed"), meta={"raw": {k: v for k, v in resp.items() if k != "raw"}})
            content = str(resp.get("content") or "")
            return AgentResult(
                role=role,
                ok=True,
                content=content,
                model=str(resp.get("model") or req_model),
                combo=str(resp.get("model") or req_model),
                latency_ms=latency,
                usage=resp.get("usage") or {},
                meta={
                    "privacy_routed": resp.get("privacy_routed"),
                    "cache_hit": resp.get("cache_hit"),
                    "fallback_used": resp.get("fallback_used"),
                },
            )
        except Exception as e:
            latency = int((time.monotonic() - t0) * 1000)
            logger.exception("multi-agent role %s failed", role)
            return AgentResult(role=role, ok=False, content="", model=req_model, combo=req_model, latency_ms=latency, error=f"{type(e).__name__}:{e}")

    @staticmethod
    def _fmt_prior(results: list[AgentResult]) -> str:
        parts = []
        for r in results:
            parts.append(f"## {r.role} ({'ok' if r.ok else 'error'}, {r.model})\n{r.content if r.ok else r.error}")
        return "\n\n".join(parts)

    async def run_single(self, *, role: str, task: str, context: str = "", model: str | None = None) -> dict[str, Any]:
        res = await self._chat(role=role, prompt=task, context=context, model=model)
        return {"ok": res.ok, "mode": "single", "result": res.__dict__, "final": res.content, "gateway": self.gateway_base}

    async def run_pipeline(self, *, task: str, roles: list[str] | None = None, context: str = "", model_map: dict[str, str] | None = None) -> dict[str, Any]:
        roles = roles or ["planner", "coder", "reviewer"]
        model_map = model_map or {}
        results: list[AgentResult] = []
        for role in roles:
            prior = self._fmt_prior(results)
            prompt = task if not prior else f"Original task:\n{task}\n\nPrevious agent outputs:\n{prior}\n\nPerform your role ({role}) building on the above."
            res = await self._chat(role=role, prompt=prompt, context=context, model=model_map.get(role))
            results.append(res)
        ok = all(r.ok for r in results) and bool(results)
        final = results[-1].content if results else ""
        return {"ok": ok, "mode": "pipeline", "roles": roles, "steps": [r.__dict__ for r in results], "final": final, "gateway": self.gateway_base}

    async def run_parallel(self, *, task: str, roles: list[str] | None = None, context: str = "", synthesize: bool = True, model_map: dict[str, str] | None = None) -> dict[str, Any]:
        roles = roles or ["planner", "coder", "researcher"]
        model_map = model_map or {}
        async def one(role: str) -> AgentResult:
            return await self._chat(role=role, prompt=task, context=context, model=model_map.get(role))
        results = await asyncio.gather(*(one(r) for r in roles))
        steps = [r.__dict__ for r in results]
        final = ""
        synth = None
        if synthesize:
            prior = self._fmt_prior(list(results))
            synth = await self._chat(role="synthesizer", prompt=f"Original task:\n{task}\n\nAgent outputs:\n{prior}\n\nProduce the best final answer.", context=context, model=model_map.get("synthesizer"))
            steps.append(synth.__dict__)
            final = synth.content
        else:
            final = "\n\n".join(r.content for r in results if r.ok)
        ok = any(r.ok for r in results)
        return {"ok": ok, "mode": "parallel", "roles": roles, "steps": steps, "final": final, "gateway": self.gateway_base}

    async def run_debate(self, *, task: str, proposer: str = "planner", critic: str = "reviewer", rounds: int = 1, context: str = "", model_map: dict[str, str] | None = None) -> dict[str, Any]:
        model_map = model_map or {}
        rounds = max(1, min(int(rounds or 1), 3))
        results: list[AgentResult] = []
        proposal = await self._chat(role=proposer, prompt=f"Task:\n{task}\n\nProvide your best proposal/solution.", context=context, model=model_map.get(proposer))
        results.append(proposal)
        last = proposal.content
        for i in range(rounds):
            critique = await self._chat(role=critic, prompt=f"Task:\n{task}\n\nProposal:\n{last}\n\nCritique it: issues, risks, improvements.", context=context, model=model_map.get(critic))
            results.append(critique)
            revision = await self._chat(role=proposer, prompt=f"Task:\n{task}\n\nPrevious proposal:\n{last}\n\nCritique:\n{critique.content}\n\nRevise the proposal addressing the critique.", context=context, model=model_map.get(proposer))
            results.append(revision)
            last = revision.content
        synth = await self._chat(role="synthesizer", prompt=f"Task:\n{task}\n\nDebate transcript:\n{self._fmt_prior(results)}\n\nProduce the final, best answer.", context=context, model=model_map.get("synthesizer"))
        results.append(synth)
        return {"ok": synth.ok or proposal.ok, "mode": "debate", "steps": [r.__dict__ for r in results], "final": synth.content or last, "gateway": self.gateway_base}

    async def run_code(self, *, task: str, context: str = "", model_map: dict[str, str] | None = None, include_tests: bool = False) -> dict[str, Any]:
        roles = ["planner", "coder", "reviewer", "fixer"]
        if include_tests:
            roles = ["planner", "coder", "tester", "reviewer", "fixer"]
        res = await self.run_pipeline(task=(
            "Coding task (use OmniRoute gateway). Return implementation-ready output.\n"
            "Requirements: follow existing conventions, be secure, handle edge cases, include file paths for changes.\n\n"
            f"Task:\n{task}"
        ), roles=roles, context=context, model_map=model_map)
        res["mode"] = "code"
        return res

    async def orchestrate(self, *, task: str, mode: str = "pipeline", roles: list[str] | None = None, context: str = "", model_map: dict[str, str] | None = None, role: str = "coder", rounds: int = 1, synthesize: bool = True, include_tests: bool = False) -> dict[str, Any]:
        mode = (mode or "pipeline").lower().strip()
        cid = uuid.uuid4().hex
        t0 = time.monotonic()
        if mode == "single":
            out = await self.run_single(role=role, task=task, context=context, model=(model_map or {}).get(role))
        elif mode == "parallel":
            out = await self.run_parallel(task=task, roles=roles, context=context, synthesize=synthesize, model_map=model_map)
        elif mode == "debate":
            out = await self.run_debate(task=task, context=context, rounds=rounds, model_map=model_map)
        elif mode == "code":
            out = await self.run_code(task=task, context=context, model_map=model_map, include_tests=include_tests)
        elif mode == "ops":
            out = await self.run_pipeline(task=task, roles=roles or ["planner", "ops", "reviewer"], context=context, model_map=model_map)
            out["mode"] = "ops"
        else:
            out = await self.run_pipeline(task=task, roles=roles, context=context, model_map=model_map)
        out["correlation_id"] = cid
        out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        try:
            from app.nexus.event_bus.store import nexus_event
            await nexus_event("MultiAgentOrchestrated", {"mode": out.get("mode"), "ok": out.get("ok"), "roles": out.get("roles") or [role], "elapsed_ms": out.get("elapsed_ms")}, source="nexus.orchestration", entity_type="orchestration", entity_id=cid, correlation_id=cid)
        except Exception:
            pass
        return out


multi_agent_orchestrator = MultiAgentOrchestrator()
