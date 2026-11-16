# OMNI-CORE / NEXUS CLI Connectors (OmniRoute Gateway)

Unified connectors for coding/ops CLI agents to use the **OmniRoute gateway** and **NEXUS/Proxmox Manager** APIs.

- **Gateway (OpenAI-compatible):** `OMNIROUTE_BASE_URL` (default `http://192.168.176.22:20128/v1`)
- **Inference auth:** `OMNIROUTE_API_KEY` (Bearer; from OmniRoute `/connect` or v1 key)
- **NEXUS middleware API:** `OMNICORE_BASE_URL` (default `http://127.0.0.1:8002`)
- **Service auth:** `OMNICORE_API_KEY` / `X-Internal-API-Key` for protected middleware endpoints

> Keep keys out of git. Use env vars or local secret stores. Bind OmniRoute internally (LAN/localhost), not public `0.0.0.0`.

## Included connectors

| Tool | Path | Type | Notes |
|---|---|---|---|
| OpenCode | `opencode/` | plugin + config | OmniRoute provider defaults + NEXUS/Proxmox custom tools (`nexus_*`) |
| Hermes Agent | `hermes-agent/` | config snippet + tools | Custom provider `omniroute` + `nexus-cli` tool wrapper |
| Continue | `continue/` | `config.yaml` | OpenAI-compatible provider -> OmniRoute |
| Aider | `aider/` | `.aider.conf.yml` / env | `--openai-api-base` -> OmniRoute |
| Cline / Roo / compatible VSCode agents | `cline/` | settings template | OpenAI-compatible base URL + model |
| Codex CLI | `codex/` | env/profile | OpenAI-compatible endpoint vars |
| Claude Code (via compatible proxy/gateway) | `claude-code/` | env notes | Route through OmniRoute-compatible endpoint/proxy if available |
| Goose | `goose/` | profile template | OpenAI-compatible provider |
| Generic OpenAI-compatible | `generic-openai/` | env/.curl examples | Works with most CLIs/SDKs |

## Shared helper

`scripts/nexus-cli.sh` is a small authenticated wrapper for NEXUS APIs (status, events, PVE nodes, server/network/firewall plans, multi-agent orchestration). Usable as a tool backend by Hermes/OpenCode/other agents.

```bash
export OMNICORE_BASE_URL="http://<middleware-ip>:8002"
export OMNICORE_API_KEY="<INTERNAL_API_KEY_or_service_token>"
./scripts/nexus-cli.sh nexus-status
./scripts/nexus-cli.sh pve-nodes
./scripts/nexus-cli.sh pve-plan "Shopware 6 LXC 4c/4GB, performance optimiert, Middleware-Anbindung"
./scripts/nexus-cli.sh orchestrate code "Refactor and harden the PVE firewall planner"
```

## Quick start

1. Export gateway vars: `export OMNIROUTE_BASE_URL=http://192.168.176.22:20128/v1 OMNIROUTE_API_KEY=...`
2. Pick tool folder and copy/apply config (or run its install helper where provided).
3. Select OmniRoute models/combos (e.g. `auto/smart`, `auto/coding`, `omni-admin-agent`, `omni-ecom-coding-smart`).
4. (Optional) Configure `OMNICORE_*` for NEXUS/Proxmox automation tools.
