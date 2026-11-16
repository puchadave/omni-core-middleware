#!/usr/bin/env bash
# Shared helpers for NEXUS Alpine LXC provisioning on Proxmox VE.
# Source this file; it is not executable standalone.

if [ -n "${NEXUS_LIB_SOURCED:-}" ]; then
    return 0
fi
NEXUS_LIB_SOURCED=1

set -euo pipefail

TARGET_NODE="${TARGET_NODE:-LOCAL}"
SSH_USER="${SSH_USER:-root}"
CTID="${CTID:-200}"
STORAGE="${STORAGE:-local}"
NET_BRIDGE="${NET_BRIDGE:-vmbr0}"
CT_IP=""
CT_GW=""
IP_MODE="dhcp"

log_info() { printf '[INFO] %s\n' "$*"; }
log_ok() { printf '[OK]   %s\n' "$*"; }
log_warn() { printf '[WARN] %s\n' "$*" >&2; }
log_err() { printf '[ERR]  %s\n' "$*" >&2; }

die() { log_err "$*"; exit 1; }

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

is_local_target() {
    [ "${TARGET_NODE:-LOCAL}" = "LOCAL" ]
}

target_run() {
    if is_local_target; then
        "$@"
    else
        ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "${SSH_USER:-root}@${TARGET_NODE}" "$@"
    fi
}

# Execute a shell snippet inside the target LXC. Reads script from stdin.
ct_bash() {
    if is_local_target; then
        pct exec "${CTID}" -- bash -s
    else
        ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "${SSH_USER:-root}@${TARGET_NODE}" "pct exec ${CTID} -- bash -s"
    fi
}

ct_exec() {
    local snippet="$1"
    if is_local_target; then
        pct exec "${CTID}" -- bash -lc "${snippet}"
    else
        ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "${SSH_USER:-root}@${TARGET_NODE}" "pct exec ${CTID} -- bash -lc $(printf '%q' "${snippet}")"
    fi
}

ct_mkdir() {
    ct_exec "mkdir -p '$1'"
}

# Copy a local directory into the container path using tar over pct exec.
ct_sync_dir() {
    local src="$1" dst="$2"
    [ -d "${src}" ] || die "source directory not found: ${src}"
    ct_mkdir "${dst}"
    if is_local_target; then
        tar -C "${src}" -cf - . | pct exec "${CTID}" -- tar -C "${dst}" -xf -
    else
        tar -C "${src}" -cf - . | ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "${SSH_USER:-root}@${TARGET_NODE}" "pct exec ${CTID} -- tar -C ${dst} -xf -"
    fi
}

next_ctid() {
    if command -v pvesh >/dev/null 2>&1; then
        pvesh get /cluster/nextid 2>/dev/null || echo 200
    else
        echo 200
    fi
}

resolve_ctid() {
    local requested="${1:-AUTO}"
    if [ "${requested}" = "AUTO" ] || [ -z "${requested}" ]; then
        next_ctid
    else
        printf '%s' "${requested}"
    fi
}

ct_exists() {
    pct status "${CTID}" >/dev/null 2>&1
}

pick_alpine_template() {
    local tpl=""
    tpl="$(pveam available 2>/dev/null | grep -E 'alpine-3\.[0-9]+.*amd64' | head -n1 | awk '{print $1}' || true)"
    if [ -z "${tpl}" ]; then
        tpl="$(pveam available 2>/dev/null | grep -E 'alpine-3\..*amd64' | head -n1 | awk '{print $1}' || true)"
    fi
    if [ -z "${tpl}" ]; then
        die "no alpine-3.x amd64 template found in pveam available"
    fi
    printf '%s' "${tpl}"
}

ensure_template_local() {
    local tpl="$1"
    if ! pveam list local 2>/dev/null | grep -qF "${tpl}"; then
        log_info "Downloading template ${tpl} into local storage"
        pveam download local "${tpl##*:}"
    fi
}

parse_ip_spec() {
    local spec="${1:-dhcp}"
    IP_MODE=dhcp
    CT_IP=""
    CT_GW=""
    if [ "${spec}" != "dhcp" ] && [ -n "${spec}" ]; then
        IP_MODE=static
        CT_IP="${spec%%,*}"
        local rest="${spec#*,}"
        CT_GW=""
        if [ "${rest}" != "${spec}" ]; then
            case ",${rest}," in
                *,gw=*) CT_GW="${rest##*,gw=}" ;;
            esac
            CT_GW="${CT_GW#gw=}"
        fi
    fi
}

net0_spec() {
    local bridge="${NET_BRIDGE:-vmbr0}"
    if [ "${IP_MODE}" = "static" ]; then
        local gw_part=""
        [ -n "${CT_GW}" ] && gw_part=",gw=${CT_GW}"
        printf 'name=eth0,bridge=%s,ip=%s%s' "${bridge}" "${CT_IP}" "${gw_part}"
    else
        printf 'name=eth0,bridge=%s,dhcp=1' "${bridge}"
    fi
}

create_lxc() {
    local hostname="$1" cores="$2" memory="$3" disk_gb="$4"
    local template
    template="$(pick_alpine_template)"
    ensure_template_local "${template}"
    local net0
    net0="$(net0_spec)"
    log_info "Creating unprivileged Alpine LXC ${CTID} (${hostname}) on ${TARGET_NODE}"
    pct create "${CTID}" "${template}" \
        --hostname "${hostname}" \
        --cores "${cores}" \
        --memory "${memory}" \
        --swap 512 \
        --storage "${STORAGE:-local}" \
        --rootfs "${STORAGE:-local}:${disk_gb}" \
        --net0 "${net0}" \
        --ostype alpine \
        --unprivileged 1 \
        --features nesting=1,keyctl=1 \
        --onboot 1 \
        --startup order=1 \
        --password "${CT_ROOT_PASSWORD:-omniverse}" \
        --force 1
}

start_and_wait_ct() {
    if ! ct_exists; then
        die "container ${CTID} does not exist on ${TARGET_NODE}"
    fi
    if [ "$(pct status "${CTID}" 2>/dev/null | awk '{print $2}')" != "running" ]; then
        log_info "Starting LXC ${CTID}"
        pct start "${CTID}"
    fi
    local i=0
    until ct_exec "ip -4 -o addr show eth0 2>/dev/null | grep -q 'inet '" >/dev/null 2>&1; do
        i=$((i+1))
        [ "${i}" -ge 60 ] && die "LXC ${CTID} did not get an IPv4 address"
        sleep 2
    done
    sleep 2
}

get_ct_ip() {
    ct_exec "ip -4 -o addr show eth0 2>/dev/null | awk '{print \$4}' | cut -d/ -f1 | head -n1" | tr -d '\r' | head -n1
}

write_json_output() {
    local path="$1" json="$2"
    if [ -n "${path}" ]; then
        mkdir -p "$(dirname "${path}")"
        printf '%s\n' "${json}" > "${path}"
    fi
    printf '%s\n' "${json}"
}
