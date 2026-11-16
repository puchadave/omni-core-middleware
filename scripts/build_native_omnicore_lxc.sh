#!/usr/bin/env bash
#
# build_native_omnicore_lxc.sh
# Build a native Alpine amd64 LXC running OMNI-CORE / NEXUS middleware with OpenRC.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib_nexus_proxmox.sh
source "${SCRIPT_DIR}/lib_nexus_proxmox.sh"

TARGET_NODE="LOCAL"
SSH_USER="root"
CTID="AUTO"
IP_SPEC="dhcp"
STORAGE="local"
NET_BRIDGE="vmbr0"
HOSTNAME="omnicore-middleware"
CORES="4"
MEMORY="4096"
DISK_GB="16"
SOURCE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_PORT="8002"
WORKERS="2"
OMNIROUTE_URL="${OMNIROUTE_URL:-http://192.168.176.22:20128/v1}"
OMNIROUTE_API_KEY="${OMNIROUTE_API_KEY:-}"
SHOPWARE_API_URL="${SHOPWARE_API_URL:-}"
SHOPWARE_CLIENT_ID="${SHOPWARE_CLIENT_ID:-}"
SHOPWARE_CLIENT_SECRET="${SHOPWARE_CLIENT_SECRET:-}"
SESSION_SECRET=""
INTERNAL_API_KEY=""
JSON_OUT=""

usage() {
    cat <<'EOF'
Usage: build_native_omnicore_lxc.sh [options]
  --target-node LOCAL|SSH_HOST   Proxmox target (default: LOCAL)
  --ssh-user USER                SSH user for remote target (default: root)
  --ctid AUTO|ID                 Container ID (default: AUTO)
  --ip dhcp|CIDR,gw=GW           Container network config (default: dhcp)
  --storage NAME                 Proxmox storage for rootfs (default: local)
  --netbridge BRIDGE             Linux bridge (default: vmbr0)
  --hostname NAME                LXC hostname (default: omnicore-middleware)
  --cores N                      CPU cores (default: 4)
  --memory MB                    RAM MB (default: 4096)
  --disk GB                       Rootfs GB on local (default: 16)
  --source-dir PATH              Middleware source directory (default: repo root)
  --app-port PORT                Uvicorn port (default: 8002)
  --workers N                    Uvicorn workers (default: 2)
  --omniroute-url URL            OmniRoute v1 URL
  --omniroute-api-key KEY        OmniRoute API key
  --shopware-api-url URL         Shopware 6 API URL
  --shopware-client-id ID        Shopware client id
  --shopware-client-secret SEC   Shopware client secret
  --json-out PATH                Write machine-readable JSON result to PATH
  -h, --help                     Show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --target-node) TARGET_NODE="${2:?}"; shift 2 ;;
        --ssh-user) SSH_USER="${2:?}"; shift 2 ;;
        --ctid) CTID="${2:?}"; shift 2 ;;
        --ip) IP_SPEC="${2:?}"; shift 2 ;;
        --storage) STORAGE="${2:?}"; shift 2 ;;
        --netbridge) NET_BRIDGE="${2:?}"; shift 2 ;;
        --hostname) HOSTNAME="${2:?}"; shift 2 ;;
        --cores) CORES="${2:?}"; shift 2 ;;
        --memory) MEMORY="${2:?}"; shift 2 ;;
        --disk) DISK_GB="${2:?}"; shift 2 ;;
        --source-dir) SOURCE_DIR="${2:?}"; shift 2 ;;
        --app-port) APP_PORT="${2:?}"; shift 2 ;;
        --workers) WORKERS="${2:?}"; shift 2 ;;
        --omniroute-url) OMNIROUTE_URL="${2:?}"; shift 2 ;;
        --omniroute-api-key) OMNIROUTE_API_KEY="${2:?}"; shift 2 ;;
        --shopware-api-url) SHOPWARE_API_URL="${2:?}"; shift 2 ;;
        --shopware-client-id) SHOPWARE_CLIENT_ID="${2:?}"; shift 2 ;;
        --shopware-client-secret) SHOPWARE_CLIENT_SECRET="${2:?}"; shift 2 ;;
        --json-out) JSON_OUT="${2:?}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

require_cmd pct
require_cmd pveam
require_cmd jq
CTID="$(resolve_ctid "${CTID}")"
parse_ip_spec "${IP_SPEC}"
[ -d "${SOURCE_DIR}" ] || die "source directory not found: ${SOURCE_DIR}"
[ -n "${SESSION_SECRET}" ] || SESSION_SECRET="$(openssl rand -hex 32)"
[ -n "${INTERNAL_API_KEY}" ] || INTERNAL_API_KEY="$(openssl rand -hex 32)"

if ! ct_exists; then
    create_lxc "${HOSTNAME}" "${CORES}" "${MEMORY}" "${DISK_GB}"
else
    log_info "Container ${CTID} already exists; reusing it"
fi
start_and_wait_ct

BUILD_DIR="$(mktemp -d /tmp/omnicore-build.XXXXXX)"
trap 'rm -rf "${BUILD_DIR}"' EXIT

log_info "Syncing OMNI-CORE source from ${SOURCE_DIR} into LXC ${CTID}:/opt/omni-core"
ct_sync_dir "${SOURCE_DIR}" /opt/omni-core

cat > "${BUILD_DIR}/build.sh" <<BUILD
#!/usr/bin/env bash
set -euo pipefail
export HOME=/root
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
cd /opt/omni-core

if ! grep -q '^community' /etc/apk/repositories; then
    sed -i 's#^\#community#community#' /etc/apk/repositories
fi
apk update
apk add --no-cache bash curl git jq ca-certificates tzdata python3 py3-pip py3-virtualenv gcc musl-dev python3-dev libffi-dev openssl-dev libxml2-dev libxslt-dev sqlite sqlite-dev

python3 -m venv /opt/omni-core/venv
/opt/omni-core/venv/bin/pip install --upgrade pip --no-cache-dir
/opt/omni-core/venv/bin/pip install --no-cache-dir -r /opt/omni-core/requirements.txt

mkdir -p /opt/omni-core/data /opt/omni-core/logs
cat > /opt/omni-core/.env <<'ENV'
APP_PORT=${APP_PORT}
OMNIROUTE_BASE_URL=${OMNIROUTE_URL}
OMNIROUTE_API_KEY=${OMNIROUTE_API_KEY}
SESSION_SECRET=${SESSION_SECRET}
INTERNAL_API_KEY=${INTERNAL_API_KEY}
SHOPWARE_API_URL=${SHOPWARE_API_URL}
SHOPWARE_CLIENT_ID=${SHOPWARE_CLIENT_ID}
SHOPWARE_CLIENT_SECRET=${SHOPWARE_CLIENT_SECRET}
DATABASE_PATH=/opt/omni-core/data/omnicore.db
ENV
chmod 600 /opt/omni-core/.env

cat > /etc/init.d/omni-core <<'INIT'
#!/sbin/openrc-run
name="omni-core"
description="OMNI-CORE NEXUS Middleware"
command="/opt/omni-core/venv/bin/uvicorn"
command_args="main:app --host 0.0.0.0 --port ${APP_PORT} --workers ${WORKERS}"
command_background="true"
directory="/opt/omni-core"
pidfile="/run/omni-core.pid"
output_log="/opt/omni-core/logs/omni-core.log"
error_log="/opt/omni-core/logs/omni-core.err.log"
depend() {
    need net
    after omnicore-firstboot
}
INIT
chmod +x /etc/init.d/omni-core

cat > /etc/init.d/omnicore-firstboot <<'INIT'
#!/sbin/openrc-run
name="omnicore-firstboot"
description="First-boot secret rotation and DB init for OMNI-CORE clones"
command="/opt/omni-core/venv/bin/python"
command_args="/usr/local/bin/omnicore-firstboot"
command_background="false"
depend() {
    before omni-core
    need localmount
}
INIT
chmod +x /etc/init.d/omnicore-firstboot

cat > /usr/local/bin/omnicore-firstboot <<'PY'
#!/usr/bin/env python3
import os, re, subprocess, pathlib
env_path = pathlib.Path('/opt/omni-core/.env')
if env_path.exists():
    text = env_path.read_text()
    for key in ('SESSION_SECRET', 'INTERNAL_API_KEY'):
        m = re.search(rf'^{key}=(.*)$', text, re.M)
        old = (m.group(1).strip() if m else '')
        if old in ('', '__GENERATE__'):
            val = subprocess.check_output(['openssl','rand','-hex','32'], text=True).strip()
            if m:
                text = re.sub(rf'^{key}=.*$', f'{key}={val}', text, flags=re.M)
            else:
                text += f'\n{key}={val}\n'
    env_path.write_text(text)
    os.chmod(env_path, 0o600)
# Initialize SQLite schema and NEXUS event store on first boot.
env = dict(os.environ)
env['DATABASE_PATH'] = '/opt/omni-core/data/omnicore.db'
env['DATABASE_URL'] = ''
script = "import asyncio; from database import init_db; from app.nexus.event_bus.store import NexusEventStore; asyncio.run(init_db()); asyncio.run(NexusEventStore().ensure_schema())"
subprocess.run(['/opt/omni-core/venv/bin/python','-c',script], cwd='/opt/omni-core', env=env, check=False)
PY
chmod +x /usr/local/bin/omnicore-firstboot

rc-update add omni-core default
rc-service omni-core restart || rc-service omni-core start

for i in \$(seq 1 40); do
    if curl -fsS http://127.0.0.1:${APP_PORT}/health >/dev/null 2>&1; then
        echo "omnicore_ready"
        exit 0
    fi
    sleep 2
done
echo "omnicore verification failed" >&2
exit 1
BUILD
chmod +x "${BUILD_DIR}/build.sh"

log_info "Installing OMNI-CORE inside LXC ${CTID}"
ct_sync_dir "${BUILD_DIR}" /tmp/omnicore-build
ct_exec "bash /tmp/omnicore-build/build.sh"
ct_exec "rm -rf /tmp/omnicore-build"

CT_IP="$(get_ct_ip)"
[ -n "${CT_IP}" ] || die "could not determine container IP"
ct_exec "curl -fsS http://127.0.0.1:${APP_PORT}/health >/dev/null"
ct_exec "curl -fsS -H 'X-Internal-API-Key: ${INTERNAL_API_KEY}' http://127.0.0.1:${APP_PORT}/api/ai-control/test-connection >/dev/null 2>&1 || true"

JSON="$(jq -n \
    --arg node "${TARGET_NODE}" \
    --arg ctid "${CTID}" \
    --arg ip "${CT_IP}" \
    --arg hostname "${HOSTNAME}" \
    --arg app_port "${APP_PORT}" \
    --arg omniroute_url "${OMNIROUTE_URL}" \
    --arg internal_api_key "${INTERNAL_API_KEY}" \
    --arg health "http://${CT_IP}:${APP_PORT}/health" \
    --arg nexus "http://${CT_IP}:${APP_PORT}/ui/nexus" \
    --arg docs "http://${CT_IP}:${APP_PORT}/docs" \
    '{ok:true,component:"omnicore-middleware",node:$node,ctid:$ctid,ip:$ip,hostname:$hostname,app_port:$app_port,omniroute_url:$omniroute_url,internal_api_key:$internal_api_key,health:$health,nexus_ui:$nexus,docs:$docs,service:"omni-core",init:"openrc",arch:"amd64",storage:"local"}')"
write_json_output "${JSON_OUT}" "${JSON}"
log_ok "OMNI-CORE LXC ready: ${CT_IP}:${APP_PORT} (CTID ${CTID})"
