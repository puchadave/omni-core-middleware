#!/usr/bin/env bash
#
# build_native_omniroute_lxc.sh
# Build a native Alpine amd64 LXC running OmniRoute with OpenRC and first-boot re-keying.
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
HOSTNAME="omniroute-gateway"
CORES="2"
MEMORY="2048"
DISK_GB="8"
API_KEY=""
JSON_OUT=""

usage() {
    cat <<'EOF'
Usage: build_native_omniroute_lxc.sh [options]
  --target-node LOCAL|SSH_HOST   Proxmox target (default: LOCAL)
  --ssh-user USER                SSH user for remote target (default: root)
  --ctid AUTO|ID                 Container ID (default: AUTO)
  --ip dhcp|CIDR,gw=GW           Container network config (default: dhcp)
  --storage NAME                 Proxmox storage for rootfs (default: local)
  --netbridge BRIDGE             Linux bridge (default: vmbr0)
  --hostname NAME                LXC hostname (default: omniroute-gateway)
  --cores N                      CPU cores (default: 2)
  --memory MB                    RAM MB (default: 2048)
  --disk GB                       Rootfs GB on local (default: 8)
  --api-key KEY                  OmniRoute API key (generated if empty)
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
        --api-key) API_KEY="${2:?}"; shift 2 ;;
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
[ -n "${API_KEY}" ] || API_KEY="$(openssl rand -hex 32)"

if ! ct_exists; then
    create_lxc "${HOSTNAME}" "${CORES}" "${MEMORY}" "${DISK_GB}"
else
    log_info "Container ${CTID} already exists; reusing it"
fi
start_and_wait_ct

SETUP_DIR="$(mktemp -d /tmp/omniroute-build.XXXXXX)"
trap 'rm -rf "${SETUP_DIR}"' EXIT
cat > "${SETUP_DIR}/build.sh" <<BUILD
#!/usr/bin/env bash
set -euo pipefail
export HOME=/root
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

if ! grep -q '^community' /etc/apk/repositories; then
    sed -i 's#^\#community#community#' /etc/apk/repositories
fi
apk update
apk add --no-cache bash curl wget git jq ca-certificates tzdata nodejs npm gcompat python3 make g++ openssl

npm config set prefix /usr/local
npm install -g omniroute@latest

mkdir -p /root/.omniroute /opt/omniroute
chmod 700 /root/.omniroute

cat > /opt/omniroute/.env <<'ENV'
OMNIROUTE_SERVER_HOST=0.0.0.0
OMNIROUTE_PORT=20128
REQUIRE_API_KEY=true
OMNIROUTE_API_KEY=${API_KEY}
OMNIROUTE_TIMEOUT_MS=1200
OMNIROUTE_FAILOVER_TIMEOUT_MS=1400
OMNIROUTE_RETRY_429=true
OMNIROUTE_RETRY_502=true
OMNIROUTE_RETRY_503=true
OMNIROUTE_CACHE_RESPONSES=true
OMNIROUTE_CACHE_TTL_SECONDS=300
OMNIROUTE_LOCAL_ONLY=true
OMNIROUTE_STORAGE_ENCRYPTION_KEY=$(openssl rand -hex 32)
ENV
chmod 600 /opt/omniroute/.env

cat > /root/.omniroute/config.json <<'JSON'
{
  "server": {"host": "0.0.0.0", "port": 20128},
  "auth": {"requireApiKey": true},
  "timeouts": {"requestMs": 1200, "failoverMs": 1400},
  "retries": {"429": true, "502": true, "503": true, "maxAttempts": 3},
  "cache": {"enabled": true, "ttlSeconds": 300},
  "security": {"bind": "0.0.0.0", "requireApiKey": true, "localGdprIsolation": true},
  "combos": [
    {"name":"omni-admin-agent","description":"Admin copilot","targets":[{"model":"auto/smart","priority":1},{"model":"auto","priority":2},{"model":"auto/fast","priority":3}]},
    {"name":"omni-content-seo","description":"SEO/content","targets":[{"model":"auto/smart","priority":1},{"model":"auto/cheap","priority":2},{"model":"auto","priority":3}]},
    {"name":"omni-vision-photo","description":"Vision/photo","targets":[{"model":"auto/vision","priority":1},{"model":"auto/smart","priority":2}]},
    {"name":"omni-video-gen","description":"Video generation","targets":[{"model":"auto/vision","priority":1},{"model":"auto/smart","priority":2}]},
    {"name":"omni-crm-support","description":"CRM/support","targets":[{"model":"auto/smart","priority":1},{"model":"auto/fast","priority":2}]},
    {"name":"omni-local-gdpr","description":"Local GDPR-only routing","targets":[{"model":"local/gdpr","priority":1}],"config":{"localOnly":true,"cloudFallback":false}}
  ]
}
JSON
chmod 600 /root/.omniroute/config.json

cat > /etc/init.d/omniroute <<'INIT'
#!/sbin/openrc-run
name="omniroute"
description="OmniRoute AI Gateway"
command="/usr/local/bin/omniroute"
command_args="--env-file /opt/omniroute/.env"
command_background="true"
pidfile="/run/omniroute.pid"
output_log="/var/log/omniroute.log"
error_log="/var/log/omniroute.err.log"
depend() {
    need net
    after omniroute-firstboot
}
INIT
chmod +x /etc/init.d/omniroute

cat > /etc/init.d/omniroute-firstboot <<'INIT'
#!/sbin/openrc-run
name="omniroute-firstboot"
description="First-boot re-keying for OmniRoute clones"
command="/usr/local/bin/omniroute-firstboot"
command_background="false"
depend() {
    before omniroute
    need localmount
}
INIT
chmod +x /etc/init.d/omniroute-firstboot

cat > /usr/local/bin/omniroute-firstboot <<'PY'
#!/usr/bin/env python3
import os, re, subprocess, pathlib
env_path = pathlib.Path('/opt/omniroute/.env')
if env_path.exists():
    text = env_path.read_text()
    m = re.search(r'^OMNIROUTE_API_KEY=(.*)$', text, re.M)
    old = (m.group(1).strip() if m else '')
    if old in ('', '__GENERATE__'):
        key = subprocess.check_output(['openssl','rand','-hex','32'], text=True).strip()
        if m:
            text = re.sub(r'^OMNIROUTE_API_KEY=.*$', 'OMNIROUTE_API_KEY=' + key, text, flags=re.M)
        else:
            text += '\nOMNIROUTE_API_KEY=' + key + '\n'
        env_path.write_text(text)
        os.chmod(env_path, 0o600)
# Fresh SSH host keys for every clone.
subprocess.run(['ssh-keygen','-A'], check=False)
PY
chmod +x /usr/local/bin/omniroute-firstboot

: > /var/log/omniroute.log
: > /var/log/omniroute.err.log
rc-update add omniroute default
rc-service omniroute restart || rc-service omniroute start

for i in \$(seq 1 40); do
    if curl -fsS -H "Authorization: Bearer ${API_KEY}" http://127.0.0.1:20128/v1/models >/dev/null 2>&1; then
        echo "omniroute_ready"
        exit 0
    fi
    sleep 2
done
echo "omniroute verification failed" >&2
exit 1
BUILD
chmod +x "${SETUP_DIR}/build.sh"

log_info "Building OmniRoute inside LXC ${CTID}"
ct_sync_dir "${SETUP_DIR}" /tmp/omniroute-build
ct_exec "bash /tmp/omniroute-build/build.sh"
ct_exec "rm -rf /tmp/omniroute-build"

CT_IP="$(get_ct_ip)"
[ -n "${CT_IP}" ] || die "could not determine container IP"
ct_exec "curl -fsS -H 'Authorization: Bearer ${API_KEY}' http://127.0.0.1:20128/v1/models >/dev/null"

JSON="$(jq -n \
    --arg node "${TARGET_NODE}" \
    --arg ctid "${CTID}" \
    --arg ip "${CT_IP}" \
    --arg hostname "${HOSTNAME}" \
    --arg api_key "${API_KEY}" \
    --arg url "http://${CT_IP}:20128/v1" \
    --arg health "http://${CT_IP}:20128/api/health" \
    '{ok:true,component:"omniroute-gateway",node:$node,ctid:$ctid,ip:$ip,hostname:$hostname,api_key:$api_key,url:$url,health:$health,service:"omniroute",init:"openrc",arch:"amd64",storage:"local"}')"
write_json_output "${JSON_OUT}" "${JSON}"
log_ok "OmniRoute LXC ready: ${CT_IP}:20128 (CTID ${CTID})"
