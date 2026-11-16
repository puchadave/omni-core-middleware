#!/usr/bin/env bash
#
# install_pve_agent.sh - Install OMNI-CORE NEXUS PVE node agent on a Proxmox VE node.
# Run on target node as root, or remotely via: ssh root@NODE 'bash -s' < install_pve_agent.sh
#
set -euo pipefail

MIDDLEWARE_URL="${MIDDLEWARE_URL:-${1:-}}"
AGENT_TOKEN="${AGENT_TOKEN:-${2:-}}"
NODE_NAME="${NODE_NAME:-$(hostname -s 2>/dev/null || hostname)}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-60}"
ENABLE_ACTIONS="${ENABLE_ACTIONS:-1}"
AGENT_SRC_URL="${AGENT_SRC_URL:-}" # optional: URL to fetch omni-pve-agent.sh from

if [ -z "$MIDDLEWARE_URL" ] || [ -z "$AGENT_TOKEN" ]; then
  echo "Usage: MIDDLEWARE_URL=http://MW:8002 AGENT_TOKEN=... bash install_pve_agent.sh" >&2
  echo "   or: bash install_pve_agent.sh <MIDDLEWARE_URL> <AGENT_TOKEN>" >&2
  exit 1
fi

echo "[*] Installing OMNI-CORE NEXUS PVE agent on ${NODE_NAME}"
apt-get update -qq >/dev/null 2>&1 || true
apt-get install -y -qq curl python3 ca-certificates >/dev/null 2>&1 || true

install -d -m 0755 /usr/local/sbin /etc
cat >/etc/omni-pve-agent.conf <<CONF
MIDDLEWARE_URL="${MIDDLEWARE_URL}"
AGENT_TOKEN="${AGENT_TOKEN}"
NODE_NAME="${NODE_NAME}"
INTERVAL_SECONDS=${INTERVAL_SECONDS}
POLL_INTERVAL_SECONDS=30
ENABLE_ACTIONS=${ENABLE_ACTIONS}
CONF
chmod 600 /etc/omni-pve-agent.conf

if [ -n "$AGENT_SRC_URL" ]; then
  curl -fsSL "$AGENT_SRC_URL" -o /usr/local/sbin/omni-pve-agent
elif [ -f "./omni-pve-agent.sh" ]; then
  cp ./omni-pve-agent.sh /usr/local/sbin/omni-pve-agent
else
  echo "[!] Agent script not found locally and AGENT_SRC_URL not set. Place omni-pve-agent.sh next to installer or set AGENT_SRC_URL." >&2
  exit 1
fi
chmod 755 /usr/local/sbin/omni-pve-agent

cat >/etc/systemd/system/omni-pve-agent.service <<'UNIT'
[Unit]
Description=OMNI-CORE NEXUS PVE Node Agent (monitoring + authorized actions)
After=network-online.target pve-cluster.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=-/etc/omni-pve-agent.conf
ExecStart=/usr/local/sbin/omni-pve-agent daemon
Restart=always
RestartSec=10
User=root
Group=root

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now omni-pve-agent.service
sleep 2
systemctl is-active --quiet omni-pve-agent.service && echo "[OK] omni-pve-agent active" || echo "[WARN] agent not active; check: journalctl -u omni-pve-agent -n 50 --no-pager"
/usr/local/sbin/omni-pve-agent once >/dev/null 2>&1 && echo "[OK] initial heartbeat sent" || echo "[WARN] initial heartbeat failed (middleware reachable/token valid?)"
