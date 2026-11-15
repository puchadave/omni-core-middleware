#!/usr/bin/env bash
# OMNI-CORE optimized, idempotent Proxmox helper for Shopware 6 Commerce Node
# Interface as specified: install-shopware-omni.sh [options]
# Default: --dry-run if critical parameters missing (safe by design)

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$SCRIPT_DIR/deploy/shopware-proxmox"
VERSION="1.0.0"

# Defaults (overridable)
CTID=""
HOSTNAME="omni-shopware"
CORES=4
MEMORY=8192
SWAP=2048
DISK="80G"
BRIDGE="vmbr0"
IP_ADDRESS="dhcp"
GATEWAY=""
DNS="1.1.1.1 8.8.8.8"
SHOP_DOMAIN=""
SHOPWARE_VERSION="latest"
DATABASE="mariadb"
REDIS_ENABLED="true"
SEARCH_ENABLED="true"
OMNI_CORE_URL="http://127.0.0.1:8002"
DRY_RUN=0
DEBUG=0
FORCE=0
UPGRADE=0
UNINSTALL=0
CONFIG_OUT=""
EXPLICIT_CRITICAL=0

usage() {
  cat <<'EOF'
Usage: ./install-shopware-omni.sh [options]

Options:
  --ctid ID              Proxmox container ID (auto-detect next free if empty)
  --hostname NAME        Container hostname (default: omni-shopware)
  --cores N              CPU cores (default: 4)
  --memory MB            RAM in MB (default: 8192)
  --swap MB              Swap in MB (default: 2048)
  --disk SIZE            Disk size, e.g. 80G (default: 80G)
  --bridge BRIDGE        Proxmox bridge (default: vmbr0)
  --ip CIDR|dhcp         IP address/CIDR or dhcp (default: dhcp)
  --gateway IP           Gateway (required for static IP)
  --dns "SERVERS"        DNS servers
  --shop-domain DOMAIN   Public/internal shop domain (empty => internal IP)
  --shopware-version V   Shopware version or 'latest' (default: latest)
  --database TYPE        Database engine (mariadb, default: mariadb)
  --redis BOOL           Enable Redis (true/false, default: true)
  --search BOOL          Enable search (true/false, default: true)
  --omni-core-url URL    OMNI-CORE base URL (default: http://127.0.0.1:8002)
  --dry-run              Inspect/validate/plan only, no changes
  --debug                Verbose logging
  --force                Allow idempotent reconfigure of existing CTID
  --upgrade              Reconfigure/upgrade existing detected node (no overwrite)
  --uninstall            Controlled uninstall of configured CTID only
  --config-out FILE      Write generated config YAML (default: temp in dry-run)
  -h, --help             Show help

Notes:
- Idempotent: existing containers/resources are never overwritten blindly.
- If critical parameters are missing, script defaults to DRY RUN and prints plan.
- Secrets are never printed. Communication with OMNI-CORE via API/webhooks only.
EOF
}

log() { echo "[INFO] $*" >&2; }
ok() { echo "[OK] $*" >&2; }
warn() { echo "[WARN] $*" >&2; }
err() { echo "[ERROR] $*" >&2; }
dbg() { [[ $DEBUG -eq 1 ]] && echo "[DEBUG] $*" >&2 || true; }
die() { err "$*"; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ctid) CTID="${2:-}"; shift 2; EXPLICIT_CRITICAL=1;;
    --hostname) HOSTNAME="${2:-}"; shift 2;;
    --cores) CORES="${2:-}"; shift 2;;
    --memory) MEMORY="${2:-}"; shift 2;;
    --swap) SWAP="${2:-}"; shift 2;;
    --disk) DISK="${2:-}"; shift 2;;
    --bridge) BRIDGE="${2:-}"; shift 2;;
    --ip) IP_ADDRESS="${2:-}"; shift 2; EXPLICIT_CRITICAL=1;;
    --gateway) GATEWAY="${2:-}"; shift 2;;
    --dns) DNS="${2:-}"; shift 2;;
    --shop-domain) SHOP_DOMAIN="${2:-}"; shift 2;;
    --shopware-version) SHOPWARE_VERSION="${2:-}"; shift 2;;
    --database) DATABASE="${2:-}"; shift 2;;
    --redis) REDIS_ENABLED="${2:-}"; shift 2;;
    --search) SEARCH_ENABLED="${2:-}"; shift 2;;
    --omni-core-url) OMNI_CORE_URL="${2:-}"; shift 2; EXPLICIT_CRITICAL=1;;
    --dry-run) DRY_RUN=1; shift;;
    --debug) DEBUG=1; shift; set -x;;
    --force) FORCE=1; shift;;
    --upgrade) UPGRADE=1; shift;;
    --uninstall) UNINSTALL=1; shift;;
    --config-out) CONFIG_OUT="${2:-}"; shift 2;;
    -h|--help) usage; exit 0;;
    *) die "Unknown argument: $1 (use --help)";;
  esac
done

# Normalize booleans
norm_bool() { case "${1,,}" in true|1|yes|on) echo true;; false|0|no|off) echo false;; *) echo "$1";; esac; }
REDIS_ENABLED="$(norm_bool "$REDIS_ENABLED")"
SEARCH_ENABLED="$(norm_bool "$SEARCH_ENABLED")"

is_proxmox_host() { have pct && [[ -d /etc/pve ]]; }

next_free_ctid() {
  if have pvesh; then
    local used next=100
    used=$(pvesh get /cluster/resources --type vm --output-format json 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print("\n".join(str(x.get("vmid")) for x in d if x.get("vmid"))) ' 2>/dev/null || true)
    while echo "$used" | grep -qx "$next"; do next=$((next+1)); done
    echo "$next"; return 0
  fi
  if have pct; then
    local next=100
    while pct status "$next" >/dev/null 2>&1; do next=$((next+1)); done
    echo "$next"; return 0
  fi
  echo ""; return 1
}

detect_host() {
  log "DISCOVER: environment and Proxmox context"
  local pve_ver node cpu ram disk_avail
  pve_ver="$(pveversion 2>/dev/null | head -1 || echo 'unknown')"
  node="$(hostname 2>/dev/null || echo 'unknown')"
  cpu="$(nproc 2>/dev/null || echo 0)"
  ram="$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)"
  disk_avail="$(df -BG /var/lib/vz 2>/dev/null | awk 'NR==2{gsub("G","",$4);print $4}' || echo '?')"
  echo "PVE: $pve_ver"
  echo "Node: $node | CPU: ${cpu} cores | RAM: ${ram} MB | Storage(free,~): ${disk_avail}G"
  if have ip; then ip -br addr 2>/dev/null | head -10 | sed 's/^/NET: /' || true; fi
  if is_proxmox_host; then
    echo "Proxmox host: yes (pct available)"
    echo "Bridges: $(ls /sys/class/net 2>/dev/null | grep -E '^vmbr' | tr '\n' ' ' || echo 'n/a')"
    echo "Storages: $(pvesm status 2>/dev/null | awk 'NR>1 && $3=="active"{print $1":"$2}' | tr '\n' ' ' || echo 'n/a')"
  else
    echo "Proxmox host: no (pct/pvesh not found). This helper must run on a Proxmox VE host to create LXC."
  fi
}

plan() {
  log "PLAN: target Shopware Commerce Node (OMNI-CORE optimized)"
  local disk_gb="${DISK%G}"; disk_gb="${disk_gb%g}"
  cat <<EOF
----------------------------------------
 OMNI-CORE SHOPWARE NODE - PLAN
----------------------------------------
CTID:        ${CTID:-"(auto-detect on Proxmox)"}
Hostname:    ${HOSTNAME}
CPU:         ${CORES} cores
RAM:         ${MEMORY} MB
SWAP:        ${SWAP} MB
Disk:        ${DISK}
Bridge:      ${BRIDGE}
IP:          ${IP_ADDRESS}
Gateway:     ${GATEWAY:-"(dhcp/auto)"}
DNS:         ${DNS}
Domain:      ${SHOP_DOMAIN:-"(internal/local IP)"}
Shopware:    ${SHOPWARE_VERSION} (stable, requirements-validated)
Database:    ${DATABASE} (production, local, non-public)
Redis:       ${REDIS_ENABLED}
Search:      ${SEARCH_ENABLED} (Shopware-compatible)
OMNI-CORE:   ${OMNI_CORE_URL} (port 8002 integration target)
Mode:        $([[ $UNINSTALL -eq 1 ]] && echo UNINSTALL || ([[ $DRY_RUN -eq 1 ]] && echo DRY-RUN || ([[ $UPGRADE -eq 1 ]] && echo UPGRADE/RECONFIGURE || echo INSTALL)))
Idempotency: enabled (no overwrite of existing resources)
Secrets:     auto-generated, never printed
Integration: API/webhooks only (no direct DB access by OMNI-CORE)
----------------------------------------
EOF
}

# Critical-params gate: default to dry-run if missing and no explicit install intent
critical_missing=0
[[ -z "$OMNI_CORE_URL" ]] && critical_missing=1
if [[ $DRY_RUN -eq 0 && $UNINSTALL -eq 0 && $EXPLICIT_CRITICAL -eq 0 ]]; then
  warn "Critical parameters not explicitly confirmed; defaulting to --dry-run (safe). Re-run with explicit options to apply."
  DRY_RUN=1
fi
if [[ "$IP_ADDRESS" != "dhcp" && -z "$GATEWAY" && $DRY_RUN -eq 0 && $UNINSTALL -eq 0 ]]; then
  die "Static IP requires --gateway."
fi

echo "=== DISCOVER ==="
detect_host || true
echo
echo "=== PLAN / DRY RUN ==="
plan

# Resolve CTID for display if possible
if [[ -z "$CTID" ]] && is_proxmox_host; then
  CTID="$(next_free_ctid || true)"
  [[ -n "$CTID" ]] && log "Auto-selected next free CTID: $CTID"
fi

# Generate effective config for deploy helper
gen_config() {
  local out="$1" disk_gb
  disk_gb="${DISK%G}"; disk_gb="${disk_gb%g}"
  local app_url="http://localhost"
  if [[ -n "$SHOP_DOMAIN" ]]; then app_url="https://${SHOP_DOMAIN}"; fi
  local ip_val="$IP_ADDRESS"
  cat > "$out" <<YAML
proxmox:
  mode: auto
  node: ""
  api_url: ""
  api_token_id: ""
  api_token_secret: ""
  verify_ssl: true
  storage: ""
  template_storage: local
  bridge: ${BRIDGE}
  unprivileged: true
container:
  ctid: ${CTID:-210}
  hostname: ${HOSTNAME}
  ostemplate: ""
  cores: ${CORES}
  memory_mb: ${MEMORY}
  swap_mb: ${SWAP}
  disk_gb: ${disk_gb}
  ip: "${ip_val}"
  gateway: "${GATEWAY}"
  dns: "${DNS}"
  onboot: true
  nesting: true
  keyctl: false
  features: "nesting=1"
  tags: "shopware,commerce,omni-core"
shopware:
  version: ${SHOPWARE_VERSION}
  channel: stable
  install_dir: /var/www/shopware
  app_url: "${app_url}"
  app_env: prod
  admin_user: admin
  admin_email: "admin@example.com"
  admin_firstname: Shopware
  admin_lastname: Admin
  admin_password: ""
  shop_name: "OMNI-CORE Shopware Node"
  shop_currency: EUR
  shop_locale: de-DE
  sales_channel_domain: "${SHOP_DOMAIN}"
database:
  type: ${DATABASE}
  version: "auto"
  name: shopware
  user: shopware
  password: ""
  host: 127.0.0.1
  port: 3306
  root_password: ""
redis:
  enabled: ${REDIS_ENABLED}
  host: 127.0.0.1
  port: 6379
  db_cache: 0
  db_session: 1
search:
  enabled: ${SEARCH_ENABLED}
middleware:
  enabled: true
  connector_dir: /opt/omni-shopware-connector
  api_username: OMNI_CORE
  api_password: ""
  webhook_base_url: "${OMNI_CORE_URL%/}/api/integrations/shopware/webhook"
  omni_core_base_url: "${OMNI_CORE_URL%/}"
  health_endpoint_enabled: true
security:
  firewall_enabled: true
  allow_http: true
  allow_https: true
  allow_ssh: true
  ssh_port: 22
  ssh_password_auth: false
  ssh_permit_root_login: prohibit-password
  fail2ban_enabled: true
  unattended_upgrades: true
  secrets_dir: /root/.omni-shopware-secrets
backup:
  enabled: true
  target_dir: /var/backups/shopware
  schedule_cron: "30 2 * * *"
  retention_days: 14
  compression: zstd
  include_media: true
  verify_integrity: true
  test_restore: false
  proxmox_vzdump: false
monitoring:
  enabled: true
  report_json: /var/log/shopware-deploy/healthcheck.json
  interval_cron: "*/5 * * * *"
  api_timeout_seconds: 10
deployment:
  log_dir: /var/log/shopware-deploy
  state_dir: /var/lib/shopware-deploy
  lock_file: /var/lock/shopware-deploy.lock
  report_file: /var/log/shopware-deploy/deployment-report.json
  require_dns_for_ssl: true
  rollback_on_failure: false
  dry_run: $([[ $DRY_RUN -eq 1 ]] && echo true || echo false)
YAML
}

cfg_path="${CONFIG_OUT:-$SCRIPT_DIR/.omni-shopware.generated.yaml}"
if [[ $DRY_RUN -eq 1 && -z "$CONFIG_OUT" ]]; then cfg_path="/tmp/omni-shopware-dryrun.yaml"; fi
mkdir -p "$(dirname "$cfg_path")" 2>/dev/null || true
gen_config "$cfg_path"
ok "Generated config: $cfg_path (OMNI-CORE: ${OMNI_CORE_URL%/}, webhook target configured)"

if [[ $DRY_RUN -eq 1 ]]; then
  ok "DRY RUN complete. No changes made."
  echo "To apply on a Proxmox VE host: $0 --ctid ${CTID:-<auto>} --hostname ${HOSTNAME} --ip ${IP_ADDRESS} --omni-core-url ${OMNI_CORE_URL} [--shop-domain <domain>] [--force|--upgrade]"
  exit 0
fi

# From here: real actions require Proxmox host
if ! is_proxmox_host; then
  die "Not running on a Proxmox VE host (pct/pvesh unavailable). Execute this script on the PVE node to provision the LXC."
fi

[[ -d "$DEPLOY_DIR" ]] || die "Deployment helpers not found: $DEPLOY_DIR"

if [[ $UNINSTALL -eq 1 ]]; then
  log "UNINSTALL: controlled removal of configured CTID only"
  [[ -n "$CTID" ]] || die "--ctid required for uninstall"
  export CONFIRM_CTID="$CTID" CONFIRM_PHRASE="DELETE-SHOPWARE"
  bash "$DEPLOY_DIR/uninstall-shopware.sh" --config "$cfg_path" --destroy-container --yes-i-know
  ok "Uninstall routine completed for CT $CTID"
  exit 0
fi

# Idempotency: detect existing
if [[ -n "$CTID" ]] && pct status "$CTID" >/dev/null 2>&1; then
  if [[ $FORCE -eq 0 && $UPGRADE -eq 0 ]]; then
    die "Container CTID $CTID already exists. Use --force to reconfigure idempotently or --upgrade, or choose another --ctid."
  fi
  warn "Existing CTID $CTID detected -> idempotent reconfigure/upgrade mode"
fi

log "IMPLEMENT: provisioning Shopware Commerce Node via deploy helper"
args=(--config "$cfg_path")
[[ $FORCE -eq 1 || $UPGRADE -eq 1 ]] && args+=(--force)
# run orchestrator
bash "$DEPLOY_DIR/deploy-shopware.sh" "${args[@]}"
rc=$?

# Post checks (best-effort) - do not fake success
echo
echo "=== VERIFY / REPORT ==="
if [[ $rc -eq 0 ]]; then
  echo "STATUS: DEPLOYMENT FINISHED (check acceptance report for SUCCESS/PARTIAL)"
else
  echo "STATUS: PARTIAL/FAILED (exit $rc)"
fi
cat <<EOF
CTID:      ${CTID:-see config}
Hostname:  ${HOSTNAME}
IP:        ${IP_ADDRESS}
OMNI-CORE: ${OMNI_CORE_URL%/}
Config:    $cfg_path
Reports:   /var/log/shopware-deploy/deployment-report.json (in CT/host per deploy), monitoring JSON per config
NOTE: Secrets never displayed. If OMNI-CORE was unreachable, integration is marked PENDING by deploy flow where supported.
EOF
exit $rc
