# Hermes Agent + OmniRoute/NEXUS Connector

## Provider (OmniRoute gateway)
1. Merge `config.omniroute.yaml` into `~/.hermes/config.yaml` (or replace `model`/`providers` accordingly).
2. Export key: `export OMNIROUTE_API_KEY=<omniroute-v1-key>`
3. Recommended base: `http://192.168.176.22:20128/v1`, model `auto/smart` or `auto/coding`.

## NEXUS tools (Proxmox Manager / multi-agent)
- Requires middleware reachable: `export OMNICORE_BASE_URL=http://<middleware-ip>:8002 OMNICORE_API_KEY=<INTERNAL_API_KEY>`
- Copy `tools/nexus_cli/` to your Hermes tools path and ensure `scripts/nexus-cli.sh` is available (set `NEXUS_CLI_SCRIPT` to absolute path if needed).
- Exposes helpers: `nexus_status`, `nexus_pve_plan`, `nexus_orchestrate` via the middleware API.

## Notes
- Hermes config lives at `~/.hermes/config.yaml`; restart Hermes after changes.
- Privacy-sensitive tasks should use `omni-local-gdpr` (middleware orchestrator enforces PII routing when requests flow through it).
