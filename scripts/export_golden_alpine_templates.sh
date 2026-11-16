#!/usr/bin/env bash
#
# export_golden_alpine_templates.sh
# Sanitize verified LXCs and export clean amd64 Proxmox templates to local storage.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib_nexus_proxmox.sh
source "${SCRIPT_DIR}/lib_nexus_proxmox.sh"

OMNIROUTE_NODE="LOCAL"
OMNIROUTE_CTID="201"
OMNICORE_NODE="LOCAL"
OMNICORE_CTID="202"
SHOPWARE_NODE="LOCAL"
SHOPWARE_CTID=""
NODE2_HOST=""
NODE2_USER="root"
TEMPLATE_DIR="/var/lib/vz/template/cache"
RESTART_AFTER_EXPORT="yes"

usage() {
    cat <<'EOF'
Usage: export_golden_alpine_templates.sh [options]
  --omniroute-node LOCAL|SSH_HOST  Node hosting OmniRoute LXC
  --omniroute-ctid ID              OmniRoute CTID
  --omnicore-node LOCAL|SSH_HOST   Node hosting OMNI-CORE LXC
  --omnicore-ctid ID               OMNI-CORE CTID
  --shopware-node LOCAL|SSH_HOST   Node hosting Shopware LXC
  --shopware-ctid ID               Shopware CTID
  --node2-host HOST                Optional Proxmox Node 2 SSH host for template sync
  --node2-user USER                SSH user for Node 2 (default: root)
  --template-dir PATH              Local template cache (default: /var/lib/vz/template/cache)
  --no-restart                     Do not restart containers after export
  -h, --help                       Show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --omniroute-node) OMNIROUTE_NODE="${2:?}"; shift 2 ;;
        --omniroute-ctid) OMNIROUTE_CTID="${2:?}"; shift 2 ;;
        --omnicore-node) OMNICORE_NODE="${2:?}"; shift 2 ;;
        --omnicore-ctid) OMNICORE_CTID="${2:?}"; shift 2 ;;
        --shopware-node) SHOPWARE_NODE="${2:?}"; shift 2 ;;
        --shopware-ctid) SHOPWARE_CTID="${2:?}"; shift 2 ;;
        --node2-host) NODE2_HOST="${2:?}"; shift 2 ;;
        --node2-user) NODE2_USER="${2:?}"; shift 2 ;;
        --template-dir) TEMPLATE_DIR="${2:?}"; shift 2 ;;
        --no-restart) RESTART_AFTER_EXPORT="no"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

require_cmd pct
require_cmd vzdump
require_cmd jq
mkdir -p "${TEMPLATE_DIR}"

node_exec() {
    local node="$1" cmd="$2"
    if [ "${node}" = "LOCAL" ]; then
        eval "${cmd}"
    else
        ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "${NODE2_USER}@${node}" "${cmd}"
    fi
}

node_copy_from() {
    local node="$1" remote="$2" local_path="$3"
    if [ "${node}" = "LOCAL" ]; then
        cp -f "${remote}" "${local_path}"
    else
        scp -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "${NODE2_USER}@${node}:${remote}" "${local_path}"
    fi
}

ct_exec_snippet_on() {
    local node="$1" ctid="$2" snippet="$3"
    if [ "${node}" = "LOCAL" ]; then
        pct exec "${ctid}" -- bash -lc "${snippet}"
    else
        ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "${NODE2_USER}@${node}" \
            "pct exec ${ctid} -- bash -lc $(printf '%q' "${snippet}")"
    fi
}

sanitize_omniroute() {
    local node="$1" ctid="$2"
    log_info "Sanitizing OmniRoute LXC ${ctid} on ${node}"
    ct_exec_snippet_on "${node}" "${ctid}" "rc-service omniroute stop >/dev/null 2>&1 || true; rc-service omniroute-firstboot stop >/dev/null 2>&1 || true"
    ct_exec_snippet_on "${node}" "${ctid}" "rm -rf /var/cache/apk/* /root/.npm/_logs/* /tmp/* /var/log/*.log /var/lib/dhcpcd/* /var/lib/udhcpc/* /etc/ssh/ssh_host_*; sed -i 's/^OMNIROUTE_API_KEY=.*/OMNIROUTE_API_KEY=__GENERATE__/' /opt/omniroute/.env; chmod 600 /opt/omniroute/.env; rc-update add omniroute-firstboot default >/dev/null 2>&1 || true"
}

sanitize_omnicore() {
    local node="$1" ctid="$2"
    log_info "Sanitizing OMNI-CORE LXC ${ctid} on ${node}"
    ct_exec_snippet_on "${node}" "${ctid}" "rc-service omni-core stop >/dev/null 2>&1 || true; rc-service omnicore-firstboot stop >/dev/null 2>&1 || true"
    ct_exec_snippet_on "${node}" "${ctid}" "rm -rf /var/cache/apk/* /tmp/* /var/log/*.log /opt/omni-core/logs/*.log /opt/omni-core/data/omnicore.db* /var/lib/dhcpcd/* /var/lib/udhcpc/* /etc/ssh/ssh_host_*; sed -i 's/^SESSION_SECRET=.*/SESSION_SECRET=__GENERATE__/; s/^INTERNAL_API_KEY=.*/INTERNAL_API_KEY=__GENERATE__/' /opt/omni-core/.env; chmod 600 /opt/omni-core/.env; rc-update add omnicore-firstboot default >/dev/null 2>&1 || true"
}

sanitize_shopware() {
    local node="$1" ctid="$2"
    log_info "Sanitizing Shopware LXC ${ctid} on ${node}"
    ct_exec_snippet_on "${node}" "${ctid}" "rc-service shopware-worker stop >/dev/null 2>&1 || true; rc-service shopware-scheduler stop >/dev/null 2>&1 || true; rc-service nginx stop >/dev/null 2>&1 || true; rc-service php-fpm83 stop >/dev/null 2>&1 || true; rc-service shopware-firstboot stop >/dev/null 2>&1 || true"
    ct_exec_snippet_on "${node}" "${ctid}" "rm -rf /var/cache/apk/* /tmp/* /var/log/*.log /var/www/shopware/var/log/*.log /var/lib/dhcpcd/* /var/lib/udhcpc/* /etc/ssh/ssh_host_*; sed -i 's/^APP_SECRET=.*/APP_SECRET=__GENERATE__/' /var/www/shopware/.env; sed -i 's/^SHOPWARE_ADMIN_PASSWORD=.*/SHOPWARE_ADMIN_PASSWORD=__GENERATE__/' /root/shopware-credentials.env; chmod 600 /var/www/shopware/.env /root/shopware-credentials.env; rc-update add shopware-firstboot default >/dev/null 2>&1 || true"
}

export_one() {
    local node="$1" ctid="$2" component="$3" target_name="$4"
    log_info "Shutting down ${component} LXC ${ctid} on ${node}"
    node_exec "${node}" "pct shutdown ${ctid} --wait 60"
    local remote_dump
    remote_dump="$(node_exec "${node}" "vzdump ${ctid} --mode stop --compress zstd --dumpdir ${TEMPLATE_DIR} --storage local 2>/dev/null | grep -o '${TEMPLATE_DIR}/vzdump-lxc-${ctid}-[^ ]*\\.tar\\.zst' | tail -n1 || true")"
    if [ -z "${remote_dump}" ]; then
        remote_dump="$(node_exec "${node}" "find ${TEMPLATE_DIR} -maxdepth 1 -type f -name 'vzdump-lxc-${ctid}-*.tar.zst' -printf '%T@ %p\\n' | sort -nr | head -n1 | cut -d' ' -f2-")"
    fi
    [ -n "${remote_dump}" ] || die "vzdump output not found for ${component} CTID ${ctid}"
    local local_dump="${TEMPLATE_DIR}/${target_name}"
    log_info "Copying ${remote_dump} -> ${local_dump}"
    node_copy_from "${node}" "${remote_dump}" "${local_dump}"
    node_exec "${node}" "rm -f '${remote_dump}'"
    if [ "${RESTART_AFTER_EXPORT}" = "yes" ]; then
        log_info "Restarting ${component} LXC ${ctid} (first-boot hooks will rotate secrets/init DB)"
        node_exec "${node}" "pct start ${ctid}"
    fi
    printf '%s' "${local_dump}"
}

sanitize_omniroute "${OMNIROUTE_NODE}" "${OMNIROUTE_CTID}"
sanitize_omnicore "${OMNICORE_NODE}" "${OMNICORE_CTID}"

OMNIROUTE_TEMPLATE="$(export_one "${OMNIROUTE_NODE}" "${OMNIROUTE_CTID}" "omniroute-gateway" "alpine-3-omniroute-gateway-amd64.tar.zst")"
OMNICORE_TEMPLATE="$(export_one "${OMNICORE_NODE}" "${OMNICORE_CTID}" "omnicore-middleware" "alpine-3-omnicore-middleware-amd64.tar.zst")"

SHOPWARE_TEMPLATE=""
SYNC_TEMPLATES=("${OMNIROUTE_TEMPLATE}" "${OMNICORE_TEMPLATE}")
if [ -n "${SHOPWARE_CTID}" ]; then
    sanitize_shopware "${SHOPWARE_NODE}" "${SHOPWARE_CTID}"
    SHOPWARE_TEMPLATE="$(export_one "${SHOPWARE_NODE}" "${SHOPWARE_CTID}" "shopware-server" "alpine-3-shopware-server-amd64.tar.zst")"
    SYNC_TEMPLATES+=("${SHOPWARE_TEMPLATE}")
fi

if [ -n "${NODE2_HOST}" ]; then
    log_info "Syncing templates to Proxmox Node 2: ${NODE2_HOST}"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "${NODE2_USER}@${NODE2_HOST}" "mkdir -p ${TEMPLATE_DIR}"
    rsync -a "${SYNC_TEMPLATES[@]}" "${NODE2_USER}@${NODE2_HOST}:${TEMPLATE_DIR}/" 2>/dev/null || \
        scp -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "${SYNC_TEMPLATES[@]}" "${NODE2_USER}@${NODE2_HOST}:${TEMPLATE_DIR}/"
    log_ok "Templates synced to Node 2"
fi

jq -n \
    --arg omniroute_template "local:vztmpl/alpine-3-omniroute-gateway-amd64.tar.zst" \
    --arg omnicore_template "local:vztmpl/alpine-3-omnicore-middleware-amd64.tar.zst" \
    --arg shopware_template "${SHOPWARE_TEMPLATE:-}" \
    --arg node2 "${NODE2_HOST}" \
    '{ok:true,templates:{omniroute:$omniroute_template,omnicore:$omnicore_template,shopware:$shopware_template},node2:$node2,arch:"amd64",storage:"local",init:"openrc"}'
