# OpenCode + OmniRoute (NEXUS) Connector

## 1) Use OmniRoute as provider
- Copy `opencode.json` to project root or merge into your global `~/.config/opencode/opencode.json`.
- Set: `export OMNIROUTE_API_KEY=<omniroute-v1-key>`
- Start/restart OpenCode, pick `omniroute/*` models (or `nexus-coder` agent).

## 2) Enable NEXUS tools plugin (optional)
- Copy `plugin/nexus-tools.ts` to `.opencode/plugin/nexus-tools.ts` (project) or global plugin dir.
- Set middleware access:
  - `export OMNICORE_BASE_URL=http://<middleware-ip>:8002`
  - `export OMNICORE_API_KEY=<INTERNAL_API_KEY>`
- Tools provided: `nexus_status`, `nexus_pve_nodes`, `nexus_pve_plan`, `nexus_pve_network_plan`, `nexus_pve_firewall_plan`, `nexus_orchestrate`.

## 3) Recommended defaults
- Base: `http://192.168.176.22:20128/v1`
- Coding: `omni-ecom-coding-smart` / `auto/coding`
- Planning/Ops: `omni-admin-agent` / `auto/smart`
- PII-sensitive: `omni-local-gdpr` (middleware orchestrator enforces privacy routing when used)
