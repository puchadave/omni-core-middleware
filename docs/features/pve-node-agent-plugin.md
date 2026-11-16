# OMNI-CORE NEXUS PVE Node Agent Plugin

Lightweight agent for Proxmox VE nodes: authenticates to middleware, reports monitoring telemetry, and executes authorized actions (tune/fix/backup/install) queued from the NEXUS UI/API.

## Middleware-side (NEXUS)
- **Auth (service principal):** token via `X-PVE-Agent-Token` (or `Authorization: Bearer <token>`). Configured as `PVE_AGENT_TOKEN` on middleware; falls back to `INTERNAL_API_KEY`. Grants access only to `/api/nexus/pve/*` telemetry/action endpoints (CSRF-exempt service path).
- **Endpoints (RBAC-protected for UI/users, token-auth for agents):**
  - `POST /api/nexus/pve/report` – agent heartbeat/telemetry -> `PveNodeHeartbeat` nexus event
  - `GET /api/nexus/pve/nodes` – latest node status (CPU/RAM/load/rootfs/storages/guests)
  - `POST /api/nexus/pve/actions` – queue action for node (`tune_node|fix_common|backup_ct|create_lxc|pct_exec|report_now`)
  - `GET /api/nexus/pve/actions/poll?node=<name>` – agent polls queued actions
  - `POST /api/nexus/pve/actions/result` – agent reports result -> `PveActionResult` event
  - `POST /api/nexus/pve/plan` – AI-assisted server plan (LXC/VM spec + commands), uses AI orchestrator when available
- **UI:** `/ui/nexus` includes PVE Nodes monitoring, AI server planner, quick-task menu, action queue, and a small assistant chat window for common tasks/fixes.

## Node agent (install on each PVE node)
Files in `scripts/pve-node-agent/`:
- `omni-pve-agent.sh` – daemon: collects metrics (`pveversion`, load/cpu/mem/swap/rootfs, `pvesh` guests/storages), posts heartbeat, polls/executes actions, posts results.
- `omni-pve-agent.service` – systemd unit.
- `omni-pve-agent.conf.example` – config template.
- `install_pve_agent.sh` – installs deps, config, script, and enables systemd service.

### Install on a PVE node (run as root)
```bash
# copy agent files to node, then:
cd /path/to/pve-node-agent
MIDDLEWARE_URL="http://<omnicore-ip>:8002" AGENT_TOKEN="<PVE_AGENT_TOKEN>" bash install_pve_agent.sh
# or set AGENT_SRC_URL to fetch omni-pve-agent.sh
```
Config: `/etc/omni-pve-agent.conf` (chmod 600). Service: `systemctl status omni-pve-agent`.

### Supported actions (agent, when `ENABLE_ACTIONS=1`)
- `tune_node` – safe/idempotent sysctl + ksmtuned/fstrim/scheduler tuning
- `fix_common` – restart failed pvedaemon/pveproxy/pvestatd, time sync, apt clean
- `backup_ct` – `vzdump <ctid>` (params: `ctid`, `mode`)
- `create_lxc` – create/start Alpine amd64 LXC on `local` (params: hostname, cores, memory_mb, disk_gb, bridge, ip)
- `pct_exec` – run command inside CT (controlled)
- `report_now` – immediate heartbeat

## Security notes
- Use a strong unique `PVE_AGENT_TOKEN`; keep middleware internal (LAN) and HTTPS/reverse-proxy in production.
- Agent runs as root on PVE nodes by necessity for Proxmox ops; restrict `ENABLE_ACTIONS=0` for monitor-only mode.
- Action queue is in-memory per middleware process; nexus events provide audit history. For multi-instance HA, back queue with DB (future hardening).
