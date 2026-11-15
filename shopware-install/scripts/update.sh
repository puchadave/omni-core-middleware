#!/usr/bin/env bash
set -Eeuo pipefail
# Reconfigure/upgrade existing Shopware Commerce Node (idempotent) via main helper
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
exec bash "$ROOT/install-shopware-omni.sh" --upgrade "$@"
