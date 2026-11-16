#!/usr/bin/env bash
#
# backup_old_containers.sh
# Backup existing/old Proxmox LXC containers via vzdump before replacement.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib_nexus_proxmox.sh
source "${SCRIPT_DIR}/lib_nexus_proxmox.sh"

TARGET_NODE="LOCAL"
SSH_USER="root"
CTIDS=""
STORAGE_DIR="/var/lib/vz/dump"
MODE="snapshot"
COMPRESS="zstd"
JSON_OUT=""

usage() {
    cat <<'EOF'
Usage: backup_old_containers.sh [options]
  --target-node LOCAL|SSH_HOST   Proxmox node hosting containers (default: LOCAL)
  --ssh-user USER                SSH user for remote target (default: root)
  --ctids "101,102,103"          Comma-separated CTIDs to back up (required)
  --storage-dir PATH             Dump directory (default: /var/lib/vz/dump)
  --mode snapshot|suspend|stop   vzdump mode (default: snapshot)
  --compress zstd|lzo|gzip|0     Compression (default: zstd)
  --json-out PATH                Write JSON result to PATH
  -h, --help                     Show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --target-node) TARGET_NODE="${2:?}"; shift 2 ;;
        --ssh-user) SSH_USER="${2:?}"; shift 2 ;;
        --ctids) CTIDS="${2:?}"; shift 2 ;;
        --storage-dir) STORAGE_DIR="${2:?}"; shift 2 ;;
        --mode) MODE="${2:?}"; shift 2 ;;
        --compress) COMPRESS="${2:?}"; shift 2 ;;
        --json-out) JSON_OUT="${2:?}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

[ -n "${CTIDS}" ] || die "--ctids is required"
require_cmd jq

run_node() {
    local cmd="$1"
    if [ "${TARGET_NODE}" = "LOCAL" ]; then
        bash -lc "${cmd}"
    else
        ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "${SSH_USER}@${TARGET_NODE}" "${cmd}"
    fi
}

IFS=',' read -r -a IDS <<< "${CTIDS}"
RESULTS=()
for raw in "${IDS[@]}"; do
    ctid="$(echo "$raw" | tr -d '[:space:]')"
    [ -n "$ctid" ] || continue
    log_info "Backing up old container CTID ${ctid} on ${TARGET_NODE} (mode=${MODE}, compress=${COMPRESS})"
    out="$(run_node "mkdir -p '${STORAGE_DIR}' && vzdump ${ctid} --mode ${MODE} --compress ${COMPRESS} --dumpdir '${STORAGE_DIR}' 2>&1 | tail -n 40" || true)"
    dump="$(printf '%s\n' "$out" | grep -oE "${STORAGE_DIR}/vzdump-lxc-${ctid}-[^ ]+\\.tar\\.(zst|lzo|gz)" | tail -n1 || true)"
    if [ -z "$dump" ]; then
        dump="$(run_node "find '${STORAGE_DIR}' -maxdepth 1 -type f -name 'vzdump-lxc-${ctid}-*' -printf '%T@ %p\\n' 2>/dev/null | sort -nr | head -n1 | cut -d' ' -f2-" || true)"
    fi
    ok="false"; [ -n "$dump" ] && ok="true"
    [ "$ok" = "true" ] && log_ok "Backup complete: ${dump}" || log_warn "Backup may have failed for CTID ${ctid}"
    RESULTS+=("$(jq -n --arg ctid "$ctid" --arg dump "$dump" --argjson ok "$ok" '{ctid:$ctid,dump:$dump,ok:$ok}')")
done

JSON="$(printf '%s\n' "${RESULTS[@]}" | jq -s '{ok: (all(.ok)), node:"'"${TARGET_NODE}"'", backups:.}')"
write_json_output "${JSON_OUT}" "${JSON}"
printf '%s\n' "${JSON}" | jq .
