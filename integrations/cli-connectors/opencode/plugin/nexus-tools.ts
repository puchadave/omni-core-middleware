import type { Plugin } from "@opencode-ai/plugin"

const OMNICORE_BASE = process.env.OMNICORE_BASE_URL || process.env.NEXUS_BASE_URL || "http://127.0.0.1:8002"
const OMNICORE_KEY = process.env.OMNICORE_API_KEY || process.env.INTERNAL_API_KEY || ""

async function nexus(path: string, init: RequestInit = {}) {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...(init.headers as any || {}) }
  if (OMNICORE_KEY) headers["X-Internal-API-Key"] = OMNICORE_KEY
  const res = await fetch(`${OMNICORE_BASE}${path}`, { ...init, headers })
  const text = await res.text()
  try { return JSON.parse(text) } catch { return { ok: res.ok, status: res.status, raw: text } }
}

export default (async () => {
  return {
    tool: {
      nexus_status: {
        description: "Get NEXUS middleware + agent status",
        args: {},
        async execute() { return await nexus("/api/nexus/status") },
      },
      nexus_pve_nodes: {
        description: "List latest Proxmox node telemetry reported by PVE agents",
        args: { limit: { type: "number", description: "max nodes/events", required: false } },
        async execute(args: any) { return await nexus(`/api/nexus/pve/nodes?limit=${args?.limit || 100}`) },
      },
      nexus_pve_plan: {
        description: "Generate AI-assisted Proxmox server plan (OmniRoute/NEXUS)",
        args: {
          requirements: { type: "string", description: "Server requirements", required: true },
          target: { type: "string", description: "lxc or vm", required: false },
          hostname: { type: "string", description: "optional hostname", required: false },
        },
        async execute(args: any) {
          return await nexus("/api/nexus/pve/plan", { method: "POST", body: JSON.stringify({ requirements: args.requirements, target: args.target || "lxc", hostname: args.hostname || "" }) })
        },
      },
      nexus_pve_network_plan: {
        description: "Generate AI-assisted Proxmox network/VLAN plan",
        args: { requirements: { type: "string", required: true }, node: { type: "string", required: false } },
        async execute(args: any) { return await nexus("/api/nexus/pve/network/plan", { method: "POST", body: JSON.stringify({ requirements: args.requirements, node: args.node || "*" }) }) },
      },
      nexus_pve_firewall_plan: {
        description: "Generate AI-assisted pve-firewall plan",
        args: { requirements: { type: "string", required: true }, scope: { type: "string", required: false }, node: { type: "string", required: false } },
        async execute(args: any) { return await nexus("/api/nexus/pve/firewall/plan", { method: "POST", body: JSON.stringify({ requirements: args.requirements, scope: args.scope || "cluster", node: args.node || "*" }) }) },
      },
      nexus_orchestrate: {
        description: "Run NEXUS multi-agent orchestration (coding/planning/ops) via OmniRoute gateway",
        args: {
          mode: { type: "string", description: "single|pipeline|parallel|debate|code|ops", required: true },
          task: { type: "string", description: "task prompt", required: true },
          context: { type: "string", description: "optional context", required: false },
        },
        async execute(args: any) { return await nexus("/api/nexus/orchestration/run", { method: "POST", body: JSON.stringify({ mode: args.mode, task: args.task, context: args.context || "" }) }) },
      },
    },
  }
}) satisfies Plugin
