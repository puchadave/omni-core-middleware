#!/usr/bin/env bash
# setup_gdrive_cluster.sh - Integrate Google Drive (whole or individual folders) as Proxmox VE cluster storages.
# Creates per-storage rclone FUSE mounts + systemd services + shared PVE dir storages in /etc/pve/storage.cfg.
#
# Examples:
#   # Preset: separate storages (iso, templates, backups, snippets, import, images/rootdir) as fixed datacenter components
#   RCLONE_CONFIG_SRC=/root/rclone.conf SSH_PASS='...' bash setup_gdrive_cluster.sh \
#     --all-nodes "192.168.178.250,192.168.178.251,192.168.178.252" \
#     --remote-name gdrive --base-path proxmox --preset datacenter
#
#   # Individual folders/storages (repeatable)
#   bash setup_gdrive_cluster.sh --all-nodes "pve01,pve02,pve03" --rclone-config /root/rclone.conf \
#     --storage "id=gdrive-iso,path=proxmox/iso,mount=/mnt/pve/gdrive-iso,content=iso,import" \
#     --storage "id=gdrive-tpl,path=proxmox/templates,mount=/mnt/pve/gdrive-tpl,content=vztmpl" \
#     --storage "id=gdrive-bak,path=proxmox/backups,mount=/mnt/pve/gdrive-bak,content=backup"
#
#   # Single node / register only
#   bash setup_gdrive_cluster.sh --node-only --storage "id=gdrive-iso,path=proxmox/iso,mount=/mnt/pve/gdrive-iso,content=iso,import"
#   bash setup_gdrive_cluster.sh --register-storage-only --storage "id=gdrive-iso,path=proxmox/iso,mount=/mnt/pve/gdrive-iso,content=iso,import"
#
set -euo pipefail

REMOTE_NAME="gdrive"
BASE_PATH=""                  # base folder in Drive (used by presets)
RCLONE_CONFIG_SRC="${RCLONE_CONFIG_SRC:-}"
RCLONE_CONFIG_DST="/root/.config/rclone/rclone.conf"
ALL_NODES=""
SSH_USER="root"
SSH_PASS="${SSH_PASS:-${SSHPASS:-}}"
NODE_ONLY="no"
REGISTER_ONLY="no"
SKIP_MOUNT="no"
VFS_CACHE_MODE="full"
CACHE_DIR="/var/cache/rclone-gdrive"
PRESET=""
# Each entry: id|remote_path|mount_point|content
STORAGE_DEFS=()

usage(){
  cat <<'EOF'
setup_gdrive_cluster.sh - Google Drive folders as Proxmox cluster storages

Options:
  --remote-name NAME        rclone remote name (default: gdrive)
  --base-path PATH          base folder in Drive for presets (e.g. proxmox)
  --preset datacenter|basic create standard per-folder storages under base-path
                             datacenter: iso(import), vztmpl, backup, snippets, images+rootdir
                             basic: iso(import), vztmpl, backup, snippets
  --storage SPEC            repeatable; SPEC: id=<id>,path=<remote_subpath>,mount=<mountpoint>,content=<csv>
                             example: id=gdrive-iso,path=proxmox/iso,mount=/mnt/pve/gdrive-iso,content=iso,import
  --rclone-config FILE      source rclone.conf to install on nodes
  --all-nodes "h1,h2,..."   orchestrate install+mount on all nodes, register storage once (cluster)
  --ssh-user USER           ssh user (default: root)
  --ssh-pass PASS           ssh password (or SSH_PASS env); prefers ssh keys if available
  --node-only               only install/mount on local node, do not register storages
  --register-storage-only    only add storage entries to /etc/pve/storage.cfg
  --skip-mount              install service/config but do not start mount
  -h, --help                show help
EOF
  exit 1
}

log(){ echo "[gdrive-cluster] $*"; }
die(){ echo "[gdrive-cluster][ERR] $*" >&2; exit 1; }
have(){ command -v "$1" >/dev/null 2>&1; }

parse_storage_spec(){
  local spec="$1" id="" path="" mount="" content=""
  IFS=',' read -r -a parts <<< "$spec"
  for p in "${parts[@]}"; do
    case "$p" in
      id=*) id="${p#id=}";;
      path=*) path="${p#path=}";;
      mount=*) mount="${p#mount=}";;
      content=*) content="${p#content=}";;
      *) ;; # ignore unknown/simple commas in content handled below
    esac
  done
  # fallback: allow content with commas by re-extracting after content=
  if [[ "$spec" == *"content="* ]]; then
    content="${spec#*content=}"
  fi
  [ -n "$id" ] || die "--storage requires id="
  [ -n "$mount" ] || mount="/mnt/pve/${id}"
  [ -n "$path" ] || path=""
  [ -n "$content" ] || content="iso,import"
  STORAGE_DEFS+=("${id}|${path}|${mount}|${content}")
}

apply_preset(){
  local base="$BASE_PATH"
  [ -n "$base" ] && base="${base%/}/"
  case "$PRESET" in
    datacenter)
      STORAGE_DEFS+=("gdrive-iso|${base}iso|/mnt/pve/gdrive-iso|iso,import")
      STORAGE_DEFS+=("gdrive-vztmpl|${base}templates|/mnt/pve/gdrive-vztmpl|vztmpl")
      STORAGE_DEFS+=("gdrive-backup|${base}backup|/mnt/pve/gdrive-backup|backup")
      STORAGE_DEFS+=("gdrive-snippets|${base}snippets|/mnt/pve/gdrive-snippets|snippets")
      STORAGE_DEFS+=("gdrive-import|${base}import|/mnt/pve/gdrive-import|import")
      STORAGE_DEFS+=("gdrive-images|${base}images|/mnt/pve/gdrive-images|images,rootdir")
      ;;
    basic)
      STORAGE_DEFS+=("gdrive-iso|${base}iso|/mnt/pve/gdrive-iso|iso,import")
      STORAGE_DEFS+=("gdrive-vztmpl|${base}templates|/mnt/pve/gdrive-vztmpl|vztmpl")
      STORAGE_DEFS+=("gdrive-backup|${base}backup|/mnt/pve/gdrive-backup|backup")
      STORAGE_DEFS+=("gdrive-snippets|${base}snippets|/mnt/pve/gdrive-snippets|snippets")
      ;;
    "") ;;
    *) die "unknown preset: $PRESET (use datacenter|basic)" ;;
  esac
}

while [ $# -gt 0 ]; do
  case "$1" in
    --remote-name) REMOTE_NAME="$2"; shift 2;;
    --base-path) BASE_PATH="$2"; shift 2;;
    --preset) PRESET="$2"; shift 2;;
    --storage) parse_storage_spec "$2"; shift 2;;
    --rclone-config) RCLONE_CONFIG_SRC="$2"; shift 2;;
    --all-nodes) ALL_NODES="$2"; shift 2;;
    --ssh-user) SSH_USER="$2"; shift 2;;
    --ssh-pass) SSH_PASS="$2"; shift 2;;
    --node-only) NODE_ONLY="yes"; shift;;
    --register-storage-only) REGISTER_ONLY="yes"; shift;;
    --skip-mount) SKIP_MOUNT="yes"; shift;;
    -h|--help) usage;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac
done

apply_preset
if [ ${#STORAGE_DEFS[@]} -eq 0 ]; then
  # backward-compatible default: single shared storage
  STORAGE_DEFS+=("${STORAGE_ID:-gdrive}|${BASE_PATH}|/mnt/pve/${STORAGE_ID:-gdrive}|iso,vztmpl,backup,snippets,import")
fi

ssh_cmd(){
  local host="$1"; shift
  if [ -n "$SSH_PASS" ] && have sshpass; then
    SSHPASS="$SSH_PASS" sshpass -e ssh -o StrictHostKeyChecking=accept-new -o PreferredAuthentications=keyboard-interactive,password -o PubkeyAuthentication=no "$SSH_USER@$host" "$@"
  else
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$SSH_USER@$host" "$@"
  fi
}
scp_cmd(){
  local src="$1" host="$2" dst="$3"
  if [ -n "$SSH_PASS" ] && have sshpass; then
    SSHPASS="$SSH_PASS" sshpass -e scp -o StrictHostKeyChecking=accept-new -o PreferredAuthentications=keyboard-interactive,password -o PubkeyAuthentication=no "$src" "$SSH_USER@$host:$dst"
  else
    scp -o StrictHostKeyChecking=accept-new "$src" "$SSH_USER@$host:$dst"
  fi
}

install_one_storage(){
  local id="$1" rpath="$2" mnt="$3" content="$4"
  local remote_spec="${REMOTE_NAME}:${rpath}"
  local cache_sub="${CACHE_DIR}/${id}"
  mkdir -p "$mnt" "$cache_sub"
  cat > "/etc/systemd/system/rclone-${id}.service" <<UNIT
[Unit]
Description=Rclone Google Drive mount (${remote_spec}) -> ${mnt} [${content}]
Wants=network-online.target
After=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/rclone mount ${remote_spec} ${mnt} \
  --config ${RCLONE_CONFIG_DST} \
  --allow-other \
  --allow-non-empty \
  --vfs-cache-mode ${VFS_CACHE_MODE} \
  --vfs-cache-max-size 20G \
  --vfs-cache-max-age 24h \
  --cache-dir ${cache_sub} \
  --dir-cache-time 72h \
  --poll-interval 60s \
  --umask 002 \
  --log-level NOTICE \
  --log-file /var/log/rclone-${id}.log
ExecStop=/bin/fusermount3 -uz ${mnt}
ExecStop=/bin/fusermount -uz ${mnt}
Restart=always
RestartSec=10
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable "rclone-${id}.service" >/dev/null 2>&1 || true
  if [ "$SKIP_MOUNT" != "yes" ] && [ -f "$RCLONE_CONFIG_DST" ] && grep -q "^\[${REMOTE_NAME}\]" "$RCLONE_CONFIG_DST"; then
    systemctl restart "rclone-${id}.service" || true
  fi
  log "configured storage ${id}: ${remote_spec} -> ${mnt} (${content})"
}

install_node(){
  log "Installing rclone/fuse prerequisites"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq rclone fuse3 ca-certificates >/dev/null 2>&1 || apt-get install -y -qq rclone fuse ca-certificates
  grep -q '^user_allow_other' /etc/fuse.conf 2>/dev/null || echo 'user_allow_other' >> /etc/fuse.conf
  sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf || true

  mkdir -p "$(dirname "$RCLONE_CONFIG_DST")" "$CACHE_DIR"
  chmod 700 "$(dirname "$RCLONE_CONFIG_DST")"
  if [ -n "$RCLONE_CONFIG_SRC" ] && [ -f "$RCLONE_CONFIG_SRC" ]; then
    install -m 600 "$RCLONE_CONFIG_SRC" "$RCLONE_CONFIG_DST"
    log "Installed rclone config to $RCLONE_CONFIG_DST"
  elif [ -f "$RCLONE_CONFIG_DST" ]; then
    log "Using existing rclone config $RCLONE_CONFIG_DST"
  else
    log "WARN: No rclone config found. Create with: rclone config (remote: $REMOTE_NAME)"
  fi

  for def in "${STORAGE_DEFS[@]}"; do
    IFS='|' read -r id rpath mnt content <<< "$def"
    install_one_storage "$id" "$rpath" "$mnt" "$content"
  done
  sleep 2
  for def in "${STORAGE_DEFS[@]}"; do
    IFS='|' read -r id rpath mnt content <<< "$def"
    if mountpoint -q "$mnt"; then log "OK mounted: $mnt"; else log "WARN not mounted: $mnt (check journalctl -u rclone-${id})"; fi
  done
}

register_storage(){
  [ -d /etc/pve ] || die "/etc/pve not found (run on a Proxmox node)"
  for def in "${STORAGE_DEFS[@]}"; do
    IFS='|' read -r id rpath mnt content <<< "$def"
    if grep -q "^dir: ${id}\b" /etc/pve/storage.cfg 2>/dev/null; then
      log "Storage '$id' already present"
    else
      cat >> /etc/pve/storage.cfg <<CFG

dir: ${id}
	path ${mnt}
	content ${content}
	shared 1
CFG
      log "Added storage '$id' -> $mnt (content: $content, shared: 1)"
    fi
  done
  log "NOTE: images/rootdir on cloud FUSE can have latency/consistency limits; prefer for backups/iso/vztmpl/snippets/import."
}

if [ "$REGISTER_ONLY" = "yes" ]; then
  register_storage
  exit 0
fi

serialize_defs(){
  local out=""
  for def in "${STORAGE_DEFS[@]}"; do
    IFS='|' read -r id rpath mnt content <<< "$def"
    out+=" --storage 'id=${id},path=${rpath},mount=${mnt},content=${content}'"
  done
  printf '%s' "$out"
}

if [ -n "$ALL_NODES" ] && [ "$NODE_ONLY" != "yes" ]; then
  IFS=',' read -r -a NODES <<< "$ALL_NODES"
  SELF_SCRIPT="$(readlink -f "$0")"
  DEFS_SERIALIZED="$(serialize_defs)"
  for n in "${NODES[@]}"; do
    n="$(echo "$n" | xargs)"; [ -z "$n" ] && continue
    log "=== Provisioning node: $n ==="
    ssh_cmd "$n" "mkdir -p /root/pve-gdrive-cluster $(dirname "$RCLONE_CONFIG_DST") $CACHE_DIR"
    scp_cmd "$SELF_SCRIPT" "$n" "/root/pve-gdrive-cluster/setup_gdrive_cluster.sh"
    if [ -n "$RCLONE_CONFIG_SRC" ] && [ -f "$RCLONE_CONFIG_SRC" ]; then
      scp_cmd "$RCLONE_CONFIG_SRC" "$n" "$RCLONE_CONFIG_DST"
      ssh_cmd "$n" "chmod 600 $RCLONE_CONFIG_DST"
    fi
    ssh_cmd "$n" "bash /root/pve-gdrive-cluster/setup_gdrive_cluster.sh --node-only --remote-name '$REMOTE_NAME' --rclone-config '$RCLONE_CONFIG_DST' $DEFS_SERIALIZED"
  done
  FIRST="${NODES[0]}"
  log "=== Registering cluster storages via $FIRST ==="
  ssh_cmd "$FIRST" "bash /root/pve-gdrive-cluster/setup_gdrive_cluster.sh --register-storage-only $DEFS_SERIALIZED"
  log "Done. Verify: pvesm status; mount | grep rclone"
  exit 0
fi

install_node
if [ "$NODE_ONLY" != "yes" ]; then
  register_storage
fi
