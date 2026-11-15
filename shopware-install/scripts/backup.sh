#!/usr/bin/env bash
set -Eeuo pipefail
CTID=""; ACTION="backup"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ctid) CTID="$2"; shift 2;;
    --restore) ACTION="restore"; shift;;
    *) echo "Usage: $0 [--ctid ID] [--restore]"; exit 2;;
  esac
done
if command -v pct >/dev/null 2>&1 && [[ -n "$CTID" ]]; then
  if [[ "$ACTION" == "backup" ]]; then pct exec "$CTID" -- /usr/local/sbin/shopware-backup.sh
  else pct exec "$CTID" -- /usr/local/sbin/shopware-restore.sh "$@"; fi
else
  if [[ "$ACTION" == "backup" ]]; then /usr/local/sbin/shopware-backup.sh; else /usr/local/sbin/shopware-restore.sh "$@"; fi
fi
