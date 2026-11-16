#!/usr/bin/env bash
#
# master_build_and_template.sh
# One-click pipeline: build native Alpine LXCs, tune/verify the pair, export golden templates.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib_nexus_proxmox.sh
source "${SCRIPT_DIR}/lib_nexus_proxmox.sh"

OMNIROUTE_NODE="LOCAL"
OMNICORE_NODE="LOCAL"
SHOPWARE_NODE="LOCAL"
SHOPWARE_CTID="AUTO"
WITH_SHOPWARE="no"
NODE2_HOST=""
NODE2_USER="root"
OMNIROUTE_CTID="AUTO"
OMNICORE_CTID="AUTO"
OMNIROUTE_IP_SPEC="dhcp"
OMNICORE_IP_SPEC="dhcp"
OMNIROUTE_API_KEY=""
SHOPWARE_API_URL=""
SHOPWARE_CLIENT_ID=""
SHOPWARE_CLIENT_SECRET=""
SOURCE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SKIP_TUNE="no"
SKIP_EXPORT="no"
BACKUP_OLD_CTIDS=""
BACKUP_NODE="LOCAL"
BACKUP_SSH_USER="root"
JSON_OUT=""

usage() {
    cat <<'EOF'
Usage: master_build_and_template.sh [options]
  --omniroute-node LOCAL|SSH_HOST  Node for OmniRoute LXC (default: LOCAL)
  --omnicore-node LOCAL|SSH_HOST   Node for OMNI-CORE LXC (default: LOCAL)
  --shopware-node LOCAL|SSH_HOST   Node for Shopware LXC (default: LOCAL)
  --with-shopware                  Also build/export the native Shopware 6 LXC
  --shopware-ctid AUTO|ID          Shopware CTID (default: AUTO)
  --node2-host HOST                Proxmox Node 2 SSH host for template sync
  --node2-user USER                SSH user for Node 2 (default: root)
  --omniroute-ctid AUTO|ID         OmniRoute CTID
  --omnicore-ctid AUTO|ID          OMNI-CORE CTID
  --omniroute-ip dhcp|CIDR,gw=GW   OmniRoute network
  --omnicore-ip dhcp|CIDR,gw=GW    OMNI-CORE network
  --omniroute-api-key KEY          Optional fixed OmniRoute API key
  --shopware-api-url URL           Shopware 6 API URL
  --shopware-client-id ID          Shopware client id
  --shopware-client-secret SEC     Shopware client secret
  --source-dir PATH                Middleware source directory
  --skip-tune                      Skip pair tuning/verification
  --skip-export                    Skip golden template export
  --backup-old-ctids "ID,ID"       Backup old containers after middleware install (vzdump)
  --backup-node LOCAL|SSH_HOST     Node hosting old containers (default: LOCAL)
  --backup-ssh-user USER           SSH user for backup node (default: root)
  --json-out PATH                  Write final JSON summary
  -h, --help                       Show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --omniroute-node) OMNIROUTE_NODE="${2:?}"; shift 2 ;;
        --omnicore-node) OMNICORE_NODE="${2:?}"; shift 2 ;;
        --shopware-node) SHOPWARE_NODE="${2:?}"; shift 2 ;;
        --with-shopware) WITH_SHOPWARE="yes"; shift ;;
        --shopware-ctid) SHOPWARE_CTID="${2:?}"; shift 2 ;;
        --node2-host) NODE2_HOST="${2:?}"; shift 2 ;;
        --node2-user) NODE2_USER="${2:?}"; shift 2 ;;
        --omniroute-ctid) OMNIROUTE_CTID="${2:?}"; shift 2 ;;
        --omnicore-ctid) OMNICORE_CTID="${2:?}"; shift 2 ;;
        --omniroute-ip) OMNIROUTE_IP_SPEC="${2:?}"; shift 2 ;;
        --omnicore-ip) OMNICORE_IP_SPEC="${2:?}"; shift 2 ;;
        --omniroute-api-key) OMNIROUTE_API_KEY="${2:?}"; shift 2 ;;
        --shopware-api-url) SHOPWARE_API_URL="${2:?}"; shift 2 ;;
        --shopware-client-id) SHOPWARE_CLIENT_ID="${2:?}"; shift 2 ;;
        --shopware-client-secret) SHOPWARE_CLIENT_SECRET="${2:?}"; shift 2 ;;
        --source-dir) SOURCE_DIR="${2:?}"; shift 2 ;;
        --skip-tune) SKIP_TUNE="yes"; shift ;;
        --skip-export) SKIP_EXPORT="yes"; shift ;;
        --backup-old-ctids) BACKUP_OLD_CTIDS="${2:?}"; shift 2 ;;
        --backup-node) BACKUP_NODE="${2:?}"; shift 2 ;;
        --backup-ssh-user) BACKUP_SSH_USER="${2:?}"; shift 2 ;;
        --json-out) JSON_OUT="${2:?}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

require_cmd jq
WORK_DIR="$(mktemp -d /tmp/nexus-master.XXXXXX)"
trap 'rm -rf "${WORK_DIR}"' EXIT
OMNIROUTE_JSON="${WORK_DIR}/omniroute.json"
OMNICORE_JSON="${WORK_DIR}/omnicore.json"
PAIR_JSON="${WORK_DIR}/pair.json"
EXPORT_JSON="${WORK_DIR}/export.json"

log_info "[1/5] Building native OmniRoute LXC"
# shellcheck disable=SC2086
bash "${SCRIPT_DIR}/build_native_omniroute_lxc.sh" \
    --target-node "${OMNIROUTE_NODE}" \
    --ctid "${OMNIROUTE_CTID}" \
    --ip "${OMNIROUTE_IP_SPEC}" \
    --storage local \
    --hostname omniroute-gateway \
    --cores 2 --memory 2048 --disk 8 \
    ${OMNIROUTE_API_KEY:+--api-key "${OMNIROUTE_API_KEY}"} \
    --json-out "${OMNIROUTE_JSON}" | tee "${WORK_DIR}/omniroute.build.log"

log_info "[2/5] Building native OMNI-CORE / NEXUS LXC"
# shellcheck disable=SC2086
bash "${SCRIPT_DIR}/build_native_omnicore_lxc.sh" \
    --target-node "${OMNICORE_NODE}" \
    --ctid "${OMNICORE_CTID}" \
    --ip "${OMNICORE_IP_SPEC}" \
    --storage local \
    --hostname omnicore-middleware \
    --cores 4 --memory 4096 --disk 16 \
    --source-dir "${SOURCE_DIR}" \
    --omniroute-url "$(jq -r '.url' "${OMNIROUTE_JSON}")" \
    --omniroute-api-key "$(jq -r '.api_key' "${OMNIROUTE_JSON}")" \
    --shopware-api-url "${SHOPWARE_API_URL}" \
    --shopware-client-id "${SHOPWARE_CLIENT_ID}" \
    --shopware-client-secret "${SHOPWARE_CLIENT_SECRET}" \
    --json-out "${OMNICORE_JSON}" | tee "${WORK_DIR}/omnicore.build.log"

BACKUP_JSON="${WORK_DIR}/backup.json"
if [ -n "${BACKUP_OLD_CTIDS}" ]; then
    log_info "[2b] Backing up old containers: ${BACKUP_OLD_CTIDS}"
    bash "${SCRIPT_DIR}/backup_old_containers.sh" \
        --target-node "${BACKUP_NODE}" \
        --ssh-user "${BACKUP_SSH_USER}" \
        --ctids "${BACKUP_OLD_CTIDS}" \
        --json-out "${BACKUP_JSON}" | tee "${WORK_DIR}/backup.log" || log_warn "Backup step reported issues; review ${WORK_DIR}/backup.log"
else
    printf '{"ok":true,"skipped":true,"backups":[]}
' > "${BACKUP_JSON}"
fi

SHOPWARE_JSON="${WORK_DIR}/shopware.json"
if [ "${WITH_SHOPWARE}" = "yes" ]; then
    log_info "[3/5] Building native Shopware 6 LXC (performance-tuned, middleware-preconfigured)"
    bash "${SCRIPT_DIR}/build_native_shopware_lxc.sh" \
        --target-node "${SHOPWARE_NODE}" \
        --ctid "${SHOPWARE_CTID}" \
        --storage local \
        --hostname shopware-server \
        --cores 4 --memory 4096 --disk 20 \
        --middleware-ip "$(jq -r '.ip' "${OMNICORE_JSON}")" \
        --middleware-port 8002 \
        --json-out "${SHOPWARE_JSON}" | tee "${WORK_DIR}/shopware.build.log"
    # Preconfigure middleware with Shopware integration credentials if generated
    SW_CREDS="$( ( [ "${SHOPWARE_NODE}" = "LOCAL" ] && pct exec "$(jq -r '.ctid' "${SHOPWARE_JSON}")" -- cat /root/shopware-credentials.env 2>/dev/null ) || true )"
    if [ -n "${SW_CREDS}" ]; then
        SW_URL="$(printf '%s\n' "${SW_CREDS}" | awk -F= '/^SHOPWARE_URL=/{print substr($0,index($0,"=")+1)}')"
        SW_ID="$(printf '%s\n' "${SW_CREDS}" | awk -F= '/^SHOPWARE_CLIENT_ID=/{print substr($0,index($0,"=")+1)}')"
        SW_SEC="$(printf '%s\n' "${SW_CREDS}" | awk -F= '/^SHOPWARE_CLIENT_SECRET=/{print substr($0,index($0,"=")+1)}')"
        log_info "Preconfiguring OMNI-CORE with Shopware integration (${SW_URL})"
        if [ "${OMNICORE_NODE}" = "LOCAL" ]; then
            pct exec "$(jq -r '.ctid' "${OMNICORE_JSON}")" -- bash -lc "cd /opt/omni-core && grep -q '^SHOPWARE_API_URL=' .env && sed -i \"s|^SHOPWARE_API_URL=.*|SHOPWARE_API_URL=${SW_URL}|; s|^SHOPWARE_CLIENT_ID=.*|SHOPWARE_CLIENT_ID=${SW_ID}|; s|^SHOPWARE_CLIENT_SECRET=.*|SHOPWARE_CLIENT_SECRET=${SW_SEC}|\" .env || printf '\nSHOPWARE_API_URL=%s\nSHOPWARE_CLIENT_ID=%s\nSHOPWARE_CLIENT_SECRET=%s\n' '${SW_URL}' '${SW_ID}' '${SW_SEC}' >> .env; rc-service omni-core restart || true"
        else
            log_warn "Remote OMNI-CORE Shopware preconfiguration skipped (non-LOCAL node); set SHOPWARE_* env manually"
        fi
        jq -n --arg url "${SW_URL}" --arg client_id "${SW_ID}" '{shopware_url:$url,shopware_client_id:$client_id,preconfigured:true}' > "${WORK_DIR}/shopware.integration.json"
    fi
else
    log_warn "Skipping Shopware build (--with-shopware not set)"
    printf '{}' > "${SHOPWARE_JSON}"
fi

if [ "${SKIP_TUNE}" != "yes" ]; then
    log_info "[4/5] Tuning and verifying OmniRoute <-> OMNI-CORE pair"
    bash "${SCRIPT_DIR}/tune_and_verify_pair.sh" \
        --omniroute-node "${OMNIROUTE_NODE}" \
        --omniroute-ctid "$(jq -r '.ctid' "${OMNIROUTE_JSON}")" \
        --omniroute-ip "$(jq -r '.ip' "${OMNIROUTE_JSON}")" \
        --omniroute-api-key "$(jq -r '.api_key' "${OMNIROUTE_JSON}")" \
        --omnicore-node "${OMNICORE_NODE}" \
        --omnicore-ctid "$(jq -r '.ctid' "${OMNICORE_JSON}")" \
        --omnicore-ip "$(jq -r '.ip' "${OMNICORE_JSON}")" \
        --internal-api-key "$(jq -r '.internal_api_key' "${OMNICORE_JSON}")" \
        --json-out "${PAIR_JSON}" | tee "${WORK_DIR}/pair.verify.log"
else
    log_warn "Skipping pair tuning/verification"
fi

if [ "${SKIP_EXPORT}" != "yes" ]; then
    log_info "[5/5] Exporting golden Alpine amd64 templates to local"
    # shellcheck disable=SC2086
    EXPORT_ARGS=(--omniroute-node "${OMNIROUTE_NODE}" --omniroute-ctid "$(jq -r '.ctid' "${OMNIROUTE_JSON}")" --omnicore-node "${OMNICORE_NODE}" --omnicore-ctid "$(jq -r '.ctid' "${OMNICORE_JSON}")" --template-dir /var/lib/vz/template/cache)
    if [ "${WITH_SHOPWARE}" = "yes" ]; then EXPORT_ARGS+=(--shopware-node "${SHOPWARE_NODE}" --shopware-ctid "$(jq -r '.ctid' "${SHOPWARE_JSON}")"); fi
    if [ -n "${NODE2_HOST}" ]; then EXPORT_ARGS+=(--node2-host "${NODE2_HOST}" --node2-user "${NODE2_USER}"); fi
    bash "${SCRIPT_DIR}/export_golden_alpine_templates.sh" "${EXPORT_ARGS[@]}" | tee "${EXPORT_JSON}"
else
    log_warn "Skipping template export"
fi

SHOPWARE_SUMMARY='null'
if [ "${WITH_SHOPWARE}" = "yes" ]; then SHOPWARE_SUMMARY="$(cat "${SHOPWARE_JSON}")"; fi
SUMMARY="$(jq -n \
    --slurpfile omniroute "${OMNIROUTE_JSON}" \
    --slurpfile omnicore "${OMNICORE_JSON}" \
    --slurpfile backup "${BACKUP_JSON}" \
    --argjson shopware "${SHOPWARE_SUMMARY}" \
    --arg node2 "${NODE2_HOST}" \
    --arg omniroute_template "local:vztmpl/alpine-3-omniroute-gateway-amd64.tar.zst" \
    --arg omnicore_template "local:vztmpl/alpine-3-omnicore-middleware-amd64.tar.zst" \
    --arg shopware_template "local:vztmpl/alpine-3-shopware-server-amd64.tar.zst" \
    '{ok:true,omniroute:$omniroute[0],omnicore:$omnicore[0],backup:$backup[0],shopware:$shopware,node2:$node2,templates:{omniroute:$omniroute_template,omnicore:$omnicore_template,shopware: (if $shopware==null then "" else $shopware_template end)},arch:"amd64",storage:"local",init:"openrc"}')"

printf '\n===== NEXUS BUILD SUMMARY =====\n'
printf '%s\n' "${SUMMARY}" | jq .
printf '\nClone on Node 1:\n'
printf 'pct create 301 %s --hostname omniroute-gateway --cores 2 --memory 2048 --storage local --rootfs local:8 --net0 name=eth0,bridge=vmbr0,ip=dhcp --ostype alpine --unprivileged 1 --features nesting=1,keyctl=1 --onboot 1\n' "local:vztmpl/alpine-3-omniroute-gateway-amd64.tar.zst"
printf 'pct create 302 %s --hostname omnicore-middleware --cores 4 --memory 4096 --storage local --rootfs local:16 --net0 name=eth0,bridge=vmbr0,ip=dhcp --ostype alpine --unprivileged 1 --features nesting=1,keyctl=1 --onboot 1\n' "local:vztmpl/alpine-3-omnicore-middleware-amd64.tar.zst"
if [ "${WITH_SHOPWARE}" = "yes" ]; then
    printf 'pct create 303 %s --hostname shopware-server --cores 4 --memory 4096 --storage local --rootfs local:20 --net0 name=eth0,bridge=vmbr0,ip=dhcp --ostype alpine --unprivileged 1 --features nesting=1,keyctl=1 --onboot 1\n' "local:vztmpl/alpine-3-shopware-server-amd64.tar.zst"
fi
if [ -n "${NODE2_HOST}" ]; then
    printf '\nTemplates were synced to Node 2: %s\n' "${NODE2_HOST}"
fi
write_json_output "${JSON_OUT}" "${SUMMARY}"
