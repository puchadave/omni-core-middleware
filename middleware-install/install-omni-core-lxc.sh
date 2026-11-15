#!/usr/bin/env bash
# OMNI-CORE Middleware - One-click Proxmox LXC installer (Docker Compose)
# Run on a Proxmox VE host as root. Safe by default: --dry-run unless --apply is set.
set -Eeuo pipefail

VERSION="1.0.0"
CTID=""; HOSTNAME="omni-core"; CORES=4; MEMORY=4096; SWAP=1024; DISK="32G"
BRIDGE="vmbr0"; IP_ADDRESS="dhcp"; GATEWAY=""; DNS="1.1.1.1 8.8.8.8"
STORAGE=""; TEMPLATE_STORAGE="local"; TEMPLATE=""
PORT="8002"; REPO_URL="https://github.com/puchadave/omni-core-middleware.git"
REPO_REF="main"; INSTALL_DIR="/opt/omni-core-middleware"
APPLY=0; DRY_RUN=0; DEBUG=0; FORCE=0; UNINSTALL=0

usage(){ cat <<'EOF'
Usage: install-omni-core-lxc.sh [options]

Provisions an LXC on Proxmox VE and deploys OMNI-CORE middleware via Docker Compose.

Options:
  --ctid ID            Container ID (auto-detect if empty)
  --hostname NAME      Hostname (default: omni-core)
  --cores N            CPU cores (default: 4)
  --memory MB          RAM in MB (default: 4096)
  --swap MB            Swap in MB (default: 1024)
  --disk SIZE          Disk size, e.g. 32G (default: 32G)
  --bridge BRIDGE      Network bridge (default: vmbr0)
  --storage NAME       Rootfs storage (auto-detect rootdir-capable if empty)
  --template-storage S Template storage (default: local)
  --template FILE      LXC template (auto-detect Debian 12 if empty)
  --ip CIDR|dhcp       IP/CIDR or dhcp (default: dhcp)
  --gateway IP         Gateway (required for static IP)
  --dns "SERVERS"      DNS servers (default: "1.1.1.1 8.8.8.8")
  --port PORT          Host port for middleware (default: 8002)
  --repo-url URL       Git repository URL
  --repo-ref REF       Git branch/tag (default: main)
  --install-dir PATH   Install dir inside CT (default: /opt/omni-core-middleware)
  --apply              Perform changes (required for install/uninstall)
  --dry-run            Plan only, no changes
  --debug              Verbose
  --force              Reconfigure/redeploy if CTID exists
  --uninstall          Stop/remove configured CTID only (requires --apply --ctid)
  -h, --help           Show help

Safety: without --apply the script runs in DRY-RUN mode. Secrets are generated inside
the container and never printed. OMNI-CORE is the orchestration layer; Shopware/AI
provider credentials must be configured separately.
EOF
}

log(){ echo "[INFO] $*" >&2; }
ok(){ echo "[OK] $*" >&2; }
warn(){ echo "[WARN] $*" >&2; }
err(){ echo "[ERROR] $*" >&2; }
die(){ err "$*"; exit 1; }
have(){ command -v "$1" >/dev/null 2>&1; }
need(){ have "$1" || die "Required command not found: $1"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ctid) CTID="${2:-}"; shift 2;;
    --hostname) HOSTNAME="${2:-}"; shift 2;;
    --cores) CORES="${2:-}"; shift 2;;
    --memory) MEMORY="${2:-}"; shift 2;;
    --swap) SWAP="${2:-}"; shift 2;;
    --disk) DISK="${2:-}"; shift 2;;
    --bridge) BRIDGE="${2:-}"; shift 2;;
    --storage) STORAGE="${2:-}"; shift 2;;
    --template-storage) TEMPLATE_STORAGE="${2:-}"; shift 2;;
    --template) TEMPLATE="${2:-}"; shift 2;;
    --ip) IP_ADDRESS="${2:-}"; shift 2;;
    --gateway) GATEWAY="${2:-}"; shift 2;;
    --dns) DNS="${2:-}"; shift 2;;
    --port) PORT="${2:-}"; shift 2;;
    --repo-url) REPO_URL="${2:-}"; shift 2;;
    --repo-ref) REPO_REF="${2:-}"; shift 2;;
    --install-dir) INSTALL_DIR="${2:-}"; shift 2;;
    --apply) APPLY=1; shift;;
    --dry-run) DRY_RUN=1; shift;;
    --debug) DEBUG=1; set -x; shift;;
    --force) FORCE=1; shift;;
    --uninstall) UNINSTALL=1; shift;;
    -h|--help) usage; exit 0;;
    *) die "Unknown argument: $1";;
  esac
done

[[ $APPLY -eq 0 ]] && DRY_RUN=1
[[ "$(id -u)" -eq 0 ]] || die "Run as root on the Proxmox VE host."
have pct || die "pct not found. Run this on a Proxmox VE host."
[[ -d /etc/pve ]] || die "/etc/pve not found. Run this on a Proxmox VE host."
need pvesm; need pveam; need python3

if [[ "$IP_ADDRESS" != "dhcp" && -z "$GATEWAY" && $UNINSTALL -eq 0 ]]; then
  die "Static IP requires --gateway."
fi

next_free_ctid(){
  local used next=100
  used=$(pvesh get /cluster/resources --type vm --output-format json 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print("\n".join(str(x.get("vmid")) for x in d if x.get("vmid")))') || true
  while echo "$used" | grep -qx "$next"; do next=$((next+1)); done
  echo "$next"
}

detect_storage(){
  local s
  s=$(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active"{print $1; exit}')
  [[ -n "$s" ]] || s="local-lvm"
  echo "$s"
}

detect_template(){
  local arch tmpl
  arch="$(dpkg --print-architecture 2>/dev/null || uname -m)"
  case "$arch" in x86_64) arch=amd64;; aarch64) arch=arm64;; esac
  pveam update >/dev/null 2>&1 || true
  tmpl=$(pveam available -section system 2>/dev/null | awk -v a="$arch" '$2 ~ /debian-12-standard_.*_'"$a"'\.tar\.(zst|xz|gz)$/ {print $2}' | tail -n1)
  [[ -n "$tmpl" ]] || tmpl=$(pveam available -section system 2>/dev/null | awk -v a="$arch" '$2 ~ /debian-.*-standard_.*_'"$a"'\.tar\.(zst|xz|gz)$/ {print $2}' | tail -n1)
  [[ -n "$tmpl" ]] || tmpl=$(pveam available -section system 2>/dev/null | awk '$2 ~ /debian-12-standard_/ {print $2}' | tail -n1)
  echo "$tmpl"
}

[[ -z "$CTID" ]] && CTID="$(next_free_ctid)"
[[ -z "$STORAGE" ]] && STORAGE="$(detect_storage)"
[[ -z "$TEMPLATE" ]] && TEMPLATE="$(detect_template)"
[[ -n "$TEMPLATE" ]] || die "No suitable Debian LXC template found. Run: pveam update"

DISK_GB="${DISK%G}"; DISK_GB="${DISK_GB%g}"
NET0="name=eth0,bridge=${BRIDGE},ip=${IP_ADDRESS}"
[[ "$IP_ADDRESS" != "dhcp" && -n "$GATEWAY" ]] && NET0+=",gw=${GATEWAY}"
TEMPLATE_PATH="${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}"

echo "=== PLAN ==="
cat <<EOF
CTID:        $CTID
Hostname:    $HOSTNAME
Resources:   ${CORES} CPU / ${MEMORY}MB RAM / ${SWAP}MB swap / ${DISK}
Storage:     $STORAGE (rootdir) | Template: $TEMPLATE_PATH
Network:     $NET0 | DNS: $DNS
Port:        $PORT -> container:8000
Repo:        $REPO_URL ($REPO_REF)
Install dir: $INSTALL_DIR (inside container)
Mode:        $([[ $UNINSTALL -eq 1 ]] && echo UNINSTALL || ([[ $DRY_RUN -eq 1 ]] && echo DRY-RUN || echo APPLY))
Idempotent:  existing CT not overwritten without --force
Secrets:     generated in-container, never printed
EOF

if [[ $DRY_RUN -eq 1 ]]; then
  ok "DRY RUN complete. Re-run with --apply to provision."
  exit 0
fi

if [[ $UNINSTALL -eq 1 ]]; then
  [[ -n "$CTID" ]] || die "--ctid required for uninstall"
  pct status "$CTID" >/dev/null 2>&1 || die "CTID $CTID not found"
  warn "Stopping and destroying CT $CTID"
  pct stop "$CTID" >/dev/null 2>&1 || true
  pct destroy "$CTID" --purge 1 >/dev/null 2>&1 || pct destroy "$CTID" >/dev/null 2>&1
  ok "CT $CTID removed"
  exit 0
fi

exists=0
pct status "$CTID" >/dev/null 2>&1 && exists=1
if [[ $exists -eq 1 && $FORCE -eq 0 ]]; then
  die "CTID $CTID already exists. Use --force to redeploy/reconfigure, or choose another --ctid."
fi

log "Ensuring template is present: $TEMPLATE"
if [[ ! -f "/var/lib/vz/template/cache/${TEMPLATE}" && ! -f "/mnt/pve/${TEMPLATE_STORAGE}/template/cache/${TEMPLATE}" ]]; then
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE" || warn "Template download reported issues; continuing if available"
fi

if [[ $exists -eq 0 ]]; then
  log "Creating LXC $CTID ($HOSTNAME)"
  pct create "$CTID" "$TEMPLATE_PATH" \
    --hostname "$HOSTNAME" --cores "$CORES" --memory "$MEMORY" --swap "$SWAP" \
    --rootfs "${STORAGE}:${DISK_GB}" --net0 "$NET0" --nameserver "$DNS" \
    --onboot 1 --unprivileged 1 --features nesting=1,keyctl=1 \
    --ostype debian --start 0
  ok "Container created"
else
  warn "CTID $CTID exists; reconfiguring (idempotent best-effort)"
  pct set "$CTID" --hostname "$HOSTNAME" --cores "$CORES" --memory "$MEMORY" --swap "$SWAP" --nameserver "$DNS" --onboot 1 >/dev/null || true
fi

pct start "$CTID" >/dev/null 2>&1 || true
sleep 5

log "Installing Docker and dependencies inside CT $CTID"
pct exec "$CTID" -- bash -lc 'set -euo pipefail; export DEBIAN_FRONTEND=noninteractive; apt-get update; apt-get install -y ca-certificates curl git openssl gnupg lsb-release; install -m0755 -d /etc/apt/keyrings; if [ ! -f /etc/apt/keyrings/docker.gpg ]; then curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg; chmod a+r /etc/apt/keyrings/docker.gpg; fi; echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list; apt-get update; apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin; systemctl enable --now docker'

log "Deploying OMNI-CORE middleware (repo: $REPO_REF)"
pct exec "$CTID" -- bash -lc "set -euo pipefail; mkdir -p '$INSTALL_DIR'; if [ -d '$INSTALL_DIR/.git' ]; then cd '$INSTALL_DIR'; git fetch --depth 1 origin '$REPO_REF' || git fetch origin '$REPO_REF' || true; git checkout '$REPO_REF' 2>/dev/null || git checkout -B '$REPO_REF' FETCH_HEAD || true; git reset --hard FETCH_HEAD 2>/dev/null || git reset --hard 'origin/$REPO_REF' 2>/dev/null || git pull --ff-only || true; else rm -rf '$INSTALL_DIR'; git clone --depth 1 --branch '$REPO_REF' '$REPO_URL' '$INSTALL_DIR'; fi"

pct exec "$CTID" -- bash -lc "set -euo pipefail; cd '$INSTALL_DIR'; if [ ! -f .env ]; then POSTGRES_PASSWORD=\$(openssl rand -hex 24); INTERNAL_API_KEY=\$(openssl rand -hex 32); SESSION_SECRET=\$(openssl rand -hex 48); cat > .env <<ENV
POSTGRES_USER=omnicore
POSTGRES_PASSWORD=\${POSTGRES_PASSWORD}
POSTGRES_DB=omnicore
OMNI_CORE_PORT=${PORT}
INTERNAL_API_KEY=\${INTERNAL_API_KEY}
SESSION_SECRET=\${SESSION_SECRET}
OMNIROUTE_URL=
OMNIROUTE_API_KEY=
MATTERHORN_API_TOKEN=
ENV
  chmod 600 .env; echo '[OK] .env created with generated secrets (not printed)'; else echo '[OK] .env exists; preserving (idempotent)'; fi; grep -q '^OMNI_CORE_PORT=' .env || echo 'OMNI_CORE_PORT=${PORT}' >> .env"

log "Starting Docker Compose stack"
pct exec "$CTID" -- bash -lc "set -euo pipefail; cd '$INSTALL_DIR'; docker compose down --remove-orphans 2>/dev/null || true; docker compose up -d --build"

log "Waiting for health endpoint"
ok_health=0
for i in $(seq 1 60); do
  if pct exec "$CTID" -- bash -lc "wget -q -O /dev/null http://127.0.0.1:8000/healthz" >/dev/null 2>&1; then ok_health=1; break; fi
  sleep 3
done

CT_IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}' || echo "<container-ip>")
echo "=== REPORT ==="
echo "STATUS:    $([[ $ok_health -eq 1 ]] && echo SUCCESS || echo PARTIAL)"
echo "CTID:      $CTID"
echo "Hostname:  $HOSTNAME"
echo "IP:        $CT_IP"
echo "URL:       http://${CT_IP}:${PORT}  (map/forward host port ${PORT} as needed)"
echo "Install:   $INSTALL_DIR (inside CT)"
echo "Health:    $([[ $ok_health -eq 1 ]] && echo 'OK (/healthz)' || echo 'PENDING - check: pct exec $CTID -- docker compose -f $INSTALL_DIR/docker-compose.yml ps')"
echo "Secrets:   stored in ${INSTALL_DIR}/.env inside CT (not displayed)"
echo "Next:      configure OMNIROUTE_URL/OMNIROUTE_API_KEY, integrations, and admin access per docs."
[[ $ok_health -eq 1 ]] || exit 2
