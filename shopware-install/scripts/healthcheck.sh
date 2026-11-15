#!/usr/bin/env bash
set -Eeuo pipefail
# Wrapper: run Shopware node healthcheck (on PVE host via pct or locally in CT)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CTID=""; CONFIG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ctid) CTID="$2"; shift 2;;
    --config) CONFIG="$2"; shift 2;;
    *) echo "Usage: $0 [--ctid ID] [--config FILE]"; exit 2;;
  esac
done
if command -v pct >/dev/null 2>&1 && [[ -n "$CTID" ]]; then
  pct exec "$CTID" -- bash -lc "cd /opt/shopware-deploy/current 2>/dev/null || cd /root; bash healthcheck.sh ${CONFIG:+--config $CONFIG}"
else
  if [[ -f "$ROOT/deploy/shopware-proxmox/healthcheck.sh" ]]; then
    (cd "$ROOT/deploy/shopware-proxmox" && bash ./healthcheck.sh ${CONFIG:+--config $CONFIG})
  else
    echo "healthcheck helper not found" >&2; exit 1
  fi
fi
