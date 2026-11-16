#!/usr/bin/env bash
#
# tune_proxmox_nodes.sh
# Apply safe performance/system tuning on Proxmox VE nodes (idempotent).
#
set -euo pipefail

NODES=""
SSH_USER="root"
JSON_OUT=""

usage() {
    cat <<'EOF'
Usage: tune_proxmox_nodes.sh [options]
  --nodes "HOST1,HOST2,..."      Comma-separated Proxmox node hosts/IPs (required)
  --ssh-user USER                SSH user (default: root)
  --json-out PATH                Write JSON summary to PATH
  -h, --help                     Show this help

Applies: sysctl tuning (vm.swappiness, dirty ratios, inotify, net buffers),
enables ksmtuned/fstrim timers when available, sets IO scheduler hints,
and runs basic PVE health checks. No data-destructive operations.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --nodes) NODES="${2:?}"; shift 2 ;;
        --ssh-user) SSH_USER="${2:?}"; shift 2 ;;
        --json-out) JSON_OUT="${2:?}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

[ -n "${NODES}" ] || { echo "--nodes is required" >&2; exit 1; }

TUNE_SCRIPT='set -euo pipefail
mkdir -p /etc/sysctl.d
cat >/etc/sysctl.d/99-omni-nexus-tuning.conf <<EOF
vm.swappiness=10
vm.dirty_ratio=15
vm.dirty_background_ratio=5
vm.vfs_cache_pressure=50
fs.inotify.max_user_watches=1048576
fs.inotify.max_user_instances=1024
net.core.somaxconn=4096
net.core.netdev_max_backlog=16384
net.ipv4.tcp_fin_timeout=15
net.ipv4.tcp_keepalive_time=600
net.ipv4.tcp_max_syn_backlog=8192
EOF
sysctl --system >/dev/null 2>&1 || sysctl -p /etc/sysctl.d/99-omni-nexus-tuning.conf >/dev/null 2>&1 || true
systemctl enable --now ksmtuned >/dev/null 2>&1 || true
systemctl enable --now fstrim.timer >/dev/null 2>&1 || true
for d in /sys/block/*/queue/scheduler; do
  if grep -q mq-deadline "$d" 2>/dev/null; then echo mq-deadline > "$d" 2>/dev/null || true; elif grep -q none "$d" 2>/dev/null; then echo none > "$d" 2>/dev/null || true; fi
done
pveversion 2>/dev/null || true
uptime
free -h | head -n2
'

IFS=',' read -r -a HOSTS <<< "${NODES}"
RESULTS=()
for raw in "${HOSTS[@]}"; do
    host="$(echo "$raw" | tr -d '[:space:]')"
    [ -n "$host" ] || continue
    echo "==> Tuning node ${host}" >&2
    if ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "${SSH_USER}@${host}" "bash -s" <<< "${TUNE_SCRIPT}" >/tmp/tune_${host//[^a-zA-Z0-9._-]/_}.log 2>&1; then
        echo "[OK] ${host}" >&2
        RESULTS+=("{\"node\":\"${host}\",\"ok\":true}")
    else
        echo "[FAIL] ${host}" >&2
        RESULTS+=("{\"node\":\"${host}\",\"ok\":false}")
    fi
done

JSON="$(printf '%s\n' "${RESULTS[@]}" | jq -s '{ok: (all(.ok)), nodes:.}')"
[ -n "${JSON_OUT}" ] && mkdir -p "$(dirname "${JSON_OUT}")" && printf '%s\n' "${JSON}" > "${JSON_OUT}"
printf '%s\n' "${JSON}" | jq .
