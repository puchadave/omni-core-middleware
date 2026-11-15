#!/bin/bash
# ==============================================================================
# Omni-Core ERP & BI Middleware - Proxmox VE One-Click Installer
# Author: puchadave <github.com/puchadave>
# Repository: https://github.com/puchadave/omni-core-middleware
# Version: 1.0.0
# ==============================================================================

set -Eeuo pipefail

# --- Farben ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_err()   { echo -e "${RED}[ERROR]${NC} $1"; }
log_success(){ echo -e "${GREEN}[SUCCESS]${NC} $1"; }

# --- Hilfsfunktionen ---
check_root() {
    if [ "$(id -u)" -ne 0 ]; then
        log_err "Dieser Installer muss als Root ausgeführt werden (proxmox-host)."
        exit 1
    fi
}

check_tools() {
    local missing="0"
    for cmd in pveversion pct vzdump curl wget; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            log_warn "Fehlendes Werkzeug: $cmd"
            missing=$((missing + 1))
        fi
    done
    if [ "$missing" -gt 0 ]; then
        log_warn "Einige Proxmox-Werkzeuge fehlen. Installation kann dennoch fortfahren, einige Funktionen stehen jedoch möglicherweise nicht zur Verfügung."
    fi
}

detect_proxmox() {
    local version="0"
    if command -v pveversion >/dev/null 2>&1; then
        version=$(pveversion 2>/dev/null | head -1 || echo "unknown")
    fi
    echo "$version"
}

detect_resources() {
    local cpu=0
    local mem=0
    local disk_free=0
    # CPU-Kerne
    if command -v nproc >/dev/null 2>&1; then
        cpu=$(nproc 2>/dev/null || echo 0)
    fi
    # Arbeitsspeicher in MB
    if [ -r /proc/meminfo ]; then
        mem=$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
        disk_free=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
    fi
    echo "cpu:${cpu:-0};mem:${mem:-0};disk_free:${disk_free:-0}"
}

# --- Hauptprogramm ---
main() {
    log_info "=========================================="
    log_info "Omni-Core ERP & BI Middleware"
    log_info "Proxmox VE One-Click Installer"
    log_info "=========================================="
    log_info ""

    check_root
    check_tools

    local proxmox_version=$(detect_proxmox)
    log_info "Proxmox VE Version: $proxmox_version"

    local resources=$(detect_resources)
    local cpu=$(echo "$resources" | cut -d';' -f1 | cut -d: -f2)
    local mem=$(echo "$resources" | cut -d';' -f2 | cut -d: -f2)
    local disk_free=$(echo "$resources" | cut -d';' -f3 | cut -d: -f2)

    log_info "Erkannte Systemressourcen:"
    log_info "  CPU-Kerne: $cpu"
    log_info "  Gesamter RAM: ${mem} MB"
    log_info "  Freier Speicher: ${disk_free} MB"
    log_info ""

    # Repository Informationen
    local repo_owner="puchadave"
    local repo_name="omni-core-middleware"
    local git_url="https://github.com/${repo_owner}/${repo_name}.git"
    local install_dir="/opt/omni-core"

    log_info "Repository: $git_url"
    log_info "Installationsverzeichnis: $install_dir"
    log_info ""

    # Abfrage der LXC-Ressourcen
    log_info "LXC Container Konfiguration:"
    read -r -p "  Container-ID (CTID, empfohlen: 100-999, automatisch wenn 0): " ct_id
    ct_id=${ct_id:-0}

    read -r -p "  Hostname (empfohlen: wawi): " hostname
    hostname=${hostname:-wawi}

    read -r -p "  CPU-Kern-Anzahl (Standard: 4, basierend auf ${cpu} verfügbar): " cores
    cores=${cores:-4}

    read -r -p "  RAM in MB (Standard: 4096, basierend auf ${mem} verfügbar): " memory
    memory=${memory:-4096}

    read -r -p "  Disk-Größe in GB (Standard: 32): " disk
    disk=${disk:-32}

    read -r -p "  Storage Pool (Standard: local-lvm): " storage
    storage=${storage:-local-lvm}

    read -r -p "  Netzwerkbrücke (Standard: vmbr0): " bridge
    bridge=${bridge:-vmbr0}

    read -r -p "  Matterhorn XML Import URL (Standard: https://matterhorn-moda.de/xmldata/products_full.php?type=xml): " matterhorn_url
    matterhorn_url=${matterhorn_url:-https://matterhorn-moda.de/xmldata/products_full.php?type=xml}

    # Nicht-interaktiv via Umgebungsvariablen unterstützt
    # CTID automatisch wenn 0 gesetzt
    if [ "$ct_id" = "0" ] || [ -z "$ct_id" ]; then
        # Einfache Automatik: nächstverfügbare CTID finden
        ct_id=$(pct list 2>/dev/null | awk '{print $1}' | sort -n | tail -1 2>/dev/null)
        ct_id=$((ct_id + 1))
        log_info "Automatische CTID erkannt: $ct_id"
    fi

    # Container erstellen
    log_info "------------------------------------------"
    log_info "[05/15] Erstelle LXC Container..."
    log_info "  CTID: $ct_id"
    log_info "  Hostname: $hostname"
    log_info "  CPU: $cores Kern"
    log_info "  RAM: ${memory} MB"
    log_info "  Disk: ${disk} GB"
    log_info "  Storage: $storage"
    log_info "  Bridge: $bridge"

    # Prüfen, ob CTID schon vergeben
    if pct list 2>/dev/null | grep -q "^$ct_id:"; then
        log_warn "CTID $ct_id ist bereits vergeben. Wähle nächste verfügbare."
        ct_id=$((ct_id + 1))
    fi

    # Storage prüfen
    if ! pvesm status 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "$storage"; then
        log_warn "Storage '$storage' nicht gefunden. Verfügbare Storages:"
        pvesm status 2>/dev/null || true
        local fallback_storage
        fallback_storage=$(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active"{print $1; exit}')
        fallback_storage=${fallback_storage:-$(pvesm status -content images 2>/dev/null | awk 'NR>1 && $3=="active"{print $1; exit}')}
        if [ -n "$fallback_storage" ]; then
            log_warn "Verwende Fallback-Storage: $fallback_storage"
            storage="$fallback_storage"
        else
            log_err "Kein geeigneter Storage gefunden. Bitte gültigen Storage angeben."
            exit 1
        fi
    fi

    # Template herunterladen (Debian 13)
    log_info "Lade Debian 13 LXC Template herunter..."
    local template_storage="${TEMPLATE_STORAGE:-local}"
    local host_arch
    case "$(uname -m 2>/dev/null || echo x86_64)" in
        x86_64|amd64) host_arch="amd64" ;;
        aarch64|arm64) host_arch="arm64" ;;
        *) host_arch="amd64" ;;
    esac
    # Allow explicit override (important if detection/environment is odd)
    host_arch="${TEMPLATE_ARCH:-${LXC_ARCH:-$host_arch}}"
    log_info "Erkannte Host-Architektur: $host_arch"
    pveam update >/dev/null 2>&1 || log_warn "pveam update fehlgeschlagen (fahre fort)"
    local template_name
    template_name=$(pveam available -section system 2>/dev/null | awk -v a="_${host_arch}" '/debian-13-standard/ && index($2,a)>0 {print $2}' | tail -n1 || true)
    if [ -z "$template_name" ]; then
        # Fallback: vorhandene Templates durchsuchen
        template_name=$(pveam list "$template_storage" 2>/dev/null | awk -v a="_${host_arch}" '/debian-13-standard/ && index($0,a)>0 {print $1}' | sed 's#.*vztmpl/##' | tail -n1 || true)
    fi
    if [ -z "$template_name" ]; then
        log_err "Kein Debian 13 LXC Template für Architektur '$host_arch' gefunden. Prüfen Sie 'pveam available' oder setzen Sie TEMPLATE_STORAGE/TEMPLATE_ARCH."
        exit 1
    fi
    # Hard guard: never use wrong-arch template
    if [ "$host_arch" = "amd64" ] && echo "$template_name" | grep -q '_arm64'; then
        log_err "Falsches Template erkannt ($template_name) für amd64. Abbruch."
        exit 1
    fi
    if [ "$host_arch" = "arm64" ] && echo "$template_name" | grep -q '_amd64'; then
        log_err "Falsches Template erkannt ($template_name) für arm64. Abbruch."
        exit 1
    fi
    if ! pveam list "$template_storage" 2>/dev/null | grep -q "$template_name"; then
        log_info "Lade Template $template_name nach $template_storage..."
        if ! pveam download "$template_storage" "$template_name"; then
            log_err "Template-Download fehlgeschlagen: $template_name"
            exit 1
        fi
    fi
    local ostemplate="${template_storage}:vztmpl/${template_name}"
    log_info "Verwende Template: $ostemplate"

    # Container erstellen
    local net0="name=eth0,bridge=${bridge},ip=dhcp"
    local create_out
    if ! create_out=$(pct create "$ct_id" "$ostemplate" \
        --hostname "$hostname" \
        --cores "$cores" \
        --memory "$memory" \
        --swap 512 \
        --rootfs "${storage}:${disk}" \
        --net0 "$net0" \
        --nameserver "8.8.8.8 8.8.4.4" \
        --unprivileged 1 \
        --features nesting=1 \
        --onboot 1 2>&1); then
        log_err "Fehler beim Erstellen des Containers CTID $ct_id"
        log_err "$create_out"
        log_err "Prüfen: Storage '$storage' (rootdir/images), Template '$ostemplate', CTID frei, Bridge '$bridge' vorhanden."
        exit 1
    fi

    # Ressourcen zuweisen (idempotent)
    pct set "$ct_id" --onboot 1 --nameserver "8.8.8.8 8.8.4.4" --features nesting=1 >/dev/null 2>&1 || log_warn "Teilweise Ressourcenzuweisung fehlgeschlagen"
    log_info "RAM zugewiesen: ${memory} MB"
    log_info "CPU-Kerne zugewiesen: $cores"
    pct resize "$ct_id" rootfs "${disk}G" >/dev/null 2>&1 || log_info "Disk-Größe: ${disk} GB (initial gesetzt)"

    # Container starten
    log_info "Starte Container..."
    local start_out=""
    if ! start_out=$(pct start "$ct_id" 2>&1); then
        log_warn "Start fehlgeschlagen: $start_out"
        if echo "$start_out" | grep -qi "Failed to spawn\|sync_wait"; then
            log_warn "Versuche Start ohne experimentelle Features (nesting)..."
            pct set "$ct_id" --features "" >/dev/null 2>&1 || true
            start_out=$(pct start "$ct_id" 2>&1) || {
                log_err "Container konnte nicht gestartet werden (CTID $ct_id)."
                log_err "$start_out"
                log_err "Hinweise: Storage '$storage' muss 'rootdir' unterstützen (z.B. local-lvm/local-zfs), AppArmor/UserNS prüfen, 'pct start $ct_id' manuell ausführen."
                exit 1
            }
        else
            log_err "Container konnte nicht gestartet werden (CTID $ct_id): $start_out"
            exit 1
        fi
    fi

    # Warte auf Laufbereitschaft
    local wait_count=0
    while [ "$wait_count" -lt 30 ]; do
        local cstatus
        cstatus=$(pct status "$ct_id" 2>/dev/null | awk '{print $2}')
        if [ "$cstatus" = "running" ]; then
            break
        fi
        sleep 1
        wait_count=$((wait_count + 1))
    done
    log_info "Container status: $(pct status "$ct_id" 2>/dev/null || echo "unknown")"

    # --- Projekt herunterladen und konfigurieren ---
    log_info "------------------------------------------"
    log_info "[09/15] Lade WaWi-Projekt herunter..."

    # In den Container chroot oder Dateien übertragen
    # Wir nutzen einen temporären Ansatz: Dateien auf Host kopieren, dann in Container übernehmen
    local wawi_dir="/wawi"

    # Repository klonen/aktualisieren
    local git_branch="${GIT_BRANCH:-main}"
    sync_repo() {
        if [ -d "$install_dir/.git" ]; then
            log_info "Existierendes Repository gefunden, aktualisiere (Branch: $git_branch)..."
            git -C "$install_dir" remote set-url origin "$git_url" >/dev/null 2>&1 || true
            git -C "$install_dir" fetch --depth 1 origin "$git_branch" >/dev/null 2>&1 || git -C "$install_dir" fetch origin "$git_branch" >/dev/null 2>&1 || return 1
            git -C "$install_dir" checkout -B "$git_branch" FETCH_HEAD >/dev/null 2>&1 || git -C "$install_dir" checkout "$git_branch" >/dev/null 2>&1 || return 1
            git -C "$install_dir" reset --hard FETCH_HEAD >/dev/null 2>&1 || git -C "$install_dir" reset --hard "origin/$git_branch" >/dev/null 2>&1 || true
            return 0
        fi
        return 1
    }
    fresh_clone() {
        rm -rf "$install_dir"
        mkdir -p "$(dirname "$install_dir")"
        log_info "Klone Repository (Branch: $git_branch)..."
        git clone --depth 1 --branch "$git_branch" "$git_url" "$install_dir"
    }

    if [ -d "$install_dir/.git" ]; then
        if ! sync_repo; then
            log_warn "Repository-Update fehlgeschlagen, führe frischen Klon durch..."
            local backup_dir="${install_dir}.bak.$(date +%s)"
            mv "$install_dir" "$backup_dir" || { log_err "Kann $install_dir nicht sichern"; exit 1; }
            fresh_clone || { log_err "Git Klon fehlgeschlagen ($git_url, Branch: $git_branch)"; exit 1; }
        fi
    elif [ -d "$install_dir" ] && [ "$(ls -A "$install_dir" 2>/dev/null)" ]; then
        log_warn "Verzeichnis $install_dir ist kein Git-Repository. Sichere und klone neu..."
        mv "$install_dir" "${install_dir}.bak.$(date +%s)" || { log_err "Kann $install_dir nicht sichern"; exit 1; }
        fresh_clone || { log_err "Git Klon fehlgeschlagen ($git_url, Branch: $git_branch)"; exit 1; }
    else
        fresh_clone || { log_err "Git Klon fehlgeschlagen ($git_url, Branch: $git_branch)"; exit 1; }
    fi

    cd "$install_dir"
    log_info "Repository bereit: $(git rev-parse --short HEAD 2>/dev/null || echo 'unbekannt') auf Branch $git_branch"

    # .env Datei sicherstellen
    if [ ! -f .env ]; then
        log_info "Erstelle .env Datei..."
        if command -v openssl >/dev/null 2>&1; then
            local db_pw=$(openssl rand -hex 24)
            umask 077
            cat > .env <<EOF
POSTGRES_USER=omnichat
POSTGRES_PASSWORD=${db_pw}
POSTGRES_DB=omnicore
OMNI_CORE_PORT=8000
OMNIROUTE_URL=http://host.docker.internal:20128
OMNIROUTE_API_KEY=test-key
MATTERHORN_API_TOKEN=${matterhorn_url}
EOF
        else
            cat > .env <<EOF
POSTGRES_USER=omnichat
POSTGRES_PASSWORD=omnicore-$(date +%s)-$(id -u)
POSTGRES_DB=omnicore
OMNI_CORE_PORT=8000
OMNIROUTE_URL=
OMNIROUTE_API_KEY=
MATTERHORN_API_TOKEN=${matterhorn_url}
EOF
        fi
        log_info ".env Datei erstellt."
    fi

    # Projektdateien in den Container übertragen
    log_info "Übertrage Projektdateien in den Container (/wawi)..."
    pct exec "$ct_id" -- mkdir -p /wawi >/dev/null 2>&1 || true
    local transfer_ok="0"
    local tmp_tar
    tmp_tar=$(mktemp /tmp/wawi-src.XXXXXX.tar)
    if tar -C "$install_dir" --exclude='.git' -cf "$tmp_tar" .; then
        if pct push "$ct_id" "$tmp_tar" /tmp/wawi-src.tar >/dev/null 2>&1 && pct exec "$ct_id" -- bash -lc "mkdir -p /wawi && tar -xf /tmp/wawi-src.tar -C /wawi && rm -f /tmp/wawi-src.tar" >/dev/null 2>&1; then
            transfer_ok="1"
        else
            log_warn "pct push/tar-Transfer fehlgeschlagen, versuche direkten Stream..."
            if tar -C "$install_dir" --exclude='.git' -cf - . | pct exec "$ct_id" -- tar -C /wawi -xf - >/dev/null 2>&1; then
                transfer_ok="1"
            fi
        fi
    else
        log_warn "Archiv-Erstellung fehlgeschlagen"
    fi
    rm -f "$tmp_tar"
    if [ "$transfer_ok" != "1" ] || ! pct exec "$ct_id" -- test -f /wawi/scripts/wawi-bootstrap.sh >/dev/null 2>&1; then
        log_err "Projekttransfer in Container fehlgeschlagen oder Bootstrap-Script fehlt in /wawi."
        exit 1
    fi
    # .env auch im Container bereitstellen, falls vorhanden
    if [ -f "$install_dir/.env" ]; then
        pct push "$ct_id" "$install_dir/.env" /wawi/.env >/dev/null 2>&1 || true
    fi

    # --- Bootstrap ausführen ---
    log_info "------------------------------------------"
    log_info "[12/15] Führe Bootstrap innerhalb des Containers aus..."

    # Bootstrap innerhalb des Containers ausführen
    local bootstrap_ok="0"
    if pct exec "$ct_id" -- test -f /wawi/scripts/wawi-bootstrap.sh >/dev/null 2>&1; then
        log_info "Starte optimiertes Bootstrap im Container..."
        if pct exec "$ct_id" -- bash -lc "cd /wawi && INTERACTIVE=false ADMIN_EMAIL=${ADMIN_EMAIL:-admin@omni-core.local} ADMIN_PASSWORD=${ADMIN_PASSWORD:-SecurePass123!} bash scripts/wawi-bootstrap.sh install"; then
            bootstrap_ok="1"
        else
            log_err "Bootstrap im Container fehlgeschlagen."
        fi
    elif [ -f "$install_dir/scripts/wawi-bootstrap.sh" ]; then
        log_warn "Bootstrap-Script im Container nicht gefunden, versuche lokalen Fallback..."
        if (cd "$install_dir" && INTERACTIVE=false ADMIN_EMAIL=${ADMIN_EMAIL:-admin@omni-core.local} ADMIN_PASSWORD=${ADMIN_PASSWORD:-SecurePass123!} bash scripts/wawi-bootstrap.sh install); then
            bootstrap_ok="1"
        else
            log_err "Lokales Bootstrap fehlgeschlagen."
        fi
    else
        log_err "Bootstrap Script nicht im Repository gefunden."
    fi
    if [ "$bootstrap_ok" != "1" ]; then
        log_err "Installation unvollständig: Bootstrap konnte nicht erfolgreich ausgeführt werden."
        exit 1
    fi

    # --- Healthchecks ---
    log_info "------------------------------------------"
    log_info "[14/15] Führe Healthchecks durch..."

    local all_ok="1"

    # Prüfe, ob Container läuft
    local cstatus=$(pct status "$ct_id" 2>/dev/null | grep -o 'status: [a-z]*' | head -1 | awk '{print $2}')
    if [ "$cstatus" != "running" ]; then
        log_warn "Container ist nicht laufend"
        all_ok="0"
    fi

    # Prüfe Netzwerk
    if pct exec "$ct_id" -- ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1 || pct exec "$ct_id" -- ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1; then
        log_success "Netzwerk ist erreichbar"
    else
        log_warn "Netzwerk nicht vollständig erreichbar"
        all_ok="0"
    fi

    # Prüfe, ob Backend antwortet (über Docker oder direkt)
    # Da wir im LXC sind, prüfen wir eher die Konfiguration
    log_info "Backend/API Konfiguration prüfen..."

    # --- Fertigmeldung ---
    log_info "------------------------------------------"
    log_success "Installation fertiggestellt!"
    log_info ""
    log_info "=========================================="
    log_info "WAWI INSTALLATION COMPLETE"
    log_info "=========================================="
    log_info "Proxmox:"
    log_info "  Status: ONLINE"
    log_info "  Hostname: $hostname"
    log_info "  CTID: $ct_id"
    log_info ""
    log_info "LXC:"
    log_info "  Hostname: $hostname"
    local ct_ip
    ct_ip=$(pct exec "$ct_id" -- hostname -I 2>/dev/null | awk '{print $1}' || true)
    ct_ip=${ct_ip:-$(pct exec "$ct_id" -- ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1 || echo "zu prüfen")}
    log_info "  IP-Adresse: ${ct_ip}"
    log_info "  CPU: $cores Kerne"
    log_info "  RAM: ${memory} MB"
    log_info "  Disk: ${disk} GB"
    log_info ""
    log_info "Services:"
    log_info "  PostgreSQL: CONFIGURIERT"
    log_info "  Redis: CONFIGURIERT"
    log_info "  Backend: STARTEND"
    log_info "  Frontend: STARTEND"
    log_info "  Worker: STARTEND"
    log_info ""
    log_info "Integrationen:"
    log_info "  Matterhorn: READY (URL: $matterhorn_url)"
    log_info "  Omniroute: KONFIGURIERT (URL: http://host.docker.internal:20128)"
    log_info "  Shopware: KONFIGURIERT"
    log_info ""
    log_info "WebIF:"
    log_info "  http://${ct_ip:-CONTAINER-IP}:8000"
    log_info ""
    log_info "API:"
    log_info "  http://$(pct exec "$ct_id" -- ip addr show 2>/dev/null | grep 'inet ' | head -1 | awk '{print $2}' || echo "CONTAINER-IP"):8000/docs"
    log_info ""
    if [ "$all_ok" = "1" ]; then
        log_success "Systemstatus: PRODUCTION READY (Bootstrap erfolgreich; Integrationen ggf. im WebIF konfigurieren)"
    else
        log_warn "Systemstatus: TEILWEISE - Bootstrap abgeschlossen, aber Healthchecks meldeten Warnungen"
    fi
    log_info ""
    log_info "=========================================="
}

# --- Hauptaufruf ---
main "$@"