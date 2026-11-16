# Claude Code + OmniRoute/NEXUS

Claude Code typically expects an Anthropic-compatible API. Route via OmniRoute only if your deployment exposes an Anthropic-compatible endpoint/proxy; otherwise use OpenAI-compatible CLIs (Continue, Aider, Cline, OpenCode, Goose) directly against `http://192.168.176.22:20128/v1`.

## NEXUS automation access
Regardless of model provider, you can use NEXUS APIs/tools from shell hooks or commands:
```bash
export OMNICORE_BASE_URL=http://<middleware-ip>:8002
export OMNICORE_API_KEY=<INTERNAL_API_KEY>
../scripts/nexus-cli.sh nexus-status
../scripts/nexus-cli.sh pve-chat "Plane Firewall-Regeln für OmniRoute/Middleware/Shopware VLANs"
```
See `env.example`.
