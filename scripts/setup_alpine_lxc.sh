#!/bin/bash
#
# setup_alpine_lxc.sh
# Fully automated bootstrap installation script for Omni-Core Middleware
# on Alpine Linux LXC container on Proxmox VE.
# This script updates packages, installs Python + venv, clones the repository,
# sets up a virtual environment with all Python dependencies, configures a
# systemd service for automatic startup, and performs a health check.
#

set -euo pipefail

# --- Color definitions for output ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# --- 1. SYSTEM-UPDATES & ABHÄNGIGKEITEN ---
log_info "Starte System-Updates und installiere Abhängigkeiten..."

apk update && apk upgrade -a --no-cache || {
    log_error "apk update/upgrade fehlgeschlagen!"
    exit 1
}

log_info "Installiere Systempakete..."
apk add --no-cache \
    python3 \
    py3-pip \
    py3-virtualenv \
    git \
    sqlite \
    curl \
    build-base \
    python3-dev \
    sqlite-libs || {
    log_error "Paket-Installation fehlgeschlagen!"
    exit 1
}

log_info "Alle Systempakete erfolgreich installiert."

# --- 2. REPOSITORY & VERZEICHNISSTRUKTUR ---
APP_DIR="/opt/omni-middleware"
REPO_URL="https://github.com/puchadave/omni-core-middleware.git"

log_info "Sichere Zielverzeichnis ${APP_DIR}..."
mkdir -p "${APP_DIR}"

log_info "Klone/repository-Repository..."
if [ -d "${APP_DIR}/.git" ]; then
    log_info "Repository existiert bereits. Update via git pull..."
    cd "${APP_DIR}" && git pull || {
        log_error "git pull fehlgeschlagen!"
        exit 1
    }
else
    log_info "Klone Repository nach ${APP_DIR}..."
    git clone "${REPO_URL}" "${APP_DIR}" || {
        log_error "git clone fehlgeschlagen!"
        exit 1
    }
fi

log_info "Repository erfolgreich gesichert/geklont."

# --- 3. PYTHON VIRTUAL ENVIRONMENT & DEPENDENCIEN ---
VENV_DIR="${APP_DIR}/venv"
log_info "Erstelle Python Virtual Environment bei ${VENV_DIR}..."
python3 -m venv "${VENV_DIR}" || {
    log_error "Virtual Environment Erstellung fehlgeschlagen!"
    exit 1
}

log_info "Aktiviere Virtual Environment und aktualisiere pip..."
# Shell-Format: source aktivierung nur für nachfolgende Befehle im Skript
# Wir nutzen den Pfad direkt, um Environment-Probleme zu vermeiden
PATH="${VENV_DIR}/bin:${PATH}"

pip install --upgrade pip || {
    log_error "pip Update fehlgeschlagen!"
    exit 1
}

log_info "Installiere Python-Pakete aus requirements.txt..."
pip install --no-cache-dir \
    fastapi==0.110.0 \
    uvicorn[standard]==0.28.0 \
    uvloop==0.19.0 \
    httpx==0.27.0 \
    pydantic==2.6.4 \
    aiosqlite==0.20.0 \
    jinja2==3.1.3 \
    python-multipart==0.0.9 || {
    log_error "Python-Pakete Installation fehlgeschlagen!"
    exit 1
}

log_info "Alle Python-Pakete erfolgreich installiert."

# --- 4. SYSTEMD SERVICE KONFIGURATION (AUTOSTART) ---
SERVICE_FILE="/etc/systemd/system/omni-middleware.service"

log_info "Erstelle systemd Service Konfiguration unter ${SERVICE_FILE}..."

cat > "${SERVICE_FILE}" << 'EOF'
[Unit]
Description=Omni-Core Middleware FastAPI Application
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=/opt/omni-middleware
ExecStart=/opt/omni-middleware/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --loop uvloop
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "${SERVICE_FILE}" || {
    log_error "Service-Datei konnte nicht beschreibbar gemacht werden!"
    exit 1
}

log_info "Reload systemd Daemon..."
systemctl daemon-reload || {
    log_warn "systemctl daemon-reload fehlgeschlagen (kann in LXC-Container ohne systemd auftreten)."
}

log_info "Aktiviere Service..."
systemctl enable omni-middleware.service || {
    log_warn "systemctl enable fehlgeschlagen (kann in LXC-Container ohne volle systemd Unterstützung auftreten)."
}

log_info "Starte Service sofort..."
systemctl start omni-middleware.service || {
    log_warn "systemctl start fehlgeschlagen (kann in LXC-Container ohne volle systemd Unterstützung auftreten)."
}

log_info "Systemd Service Konfiguration abgeschlossen."

# --- 5. HEALTH CHECK ---
log_info "Warte 3 Sekunden auf Initialisierung des Services..."
sleep 3

log_info "Führe Health Check durch..."
HEALTH_CHECK_URL="http://localhost:8000/"
RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" "${HEALTH_CHECK_URL}" || echo "000")

if [ "${RESPONSE}" = "200" ] || [ "${RESPONSE}" = "1200" ] 2>/dev/null; then
    echo -e ""
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}   Health Check erfolgreich!${NC}"
    echo -e "${GREEN}   FastAPI Dienst erreichbar unter:${NC} ${HEALTH_CHECK_URL}"
    echo -e "${GREEN}========================================${NC}"
    echo -e ""
else
    log_warn "Health Check lief mit Response-Code: ${RESPONSE}"
    log_warn "Der Dienst wird möglicherweise noch gestartet. Prüfen Sie später den Status mit: systemctl status omni-middleware.service"
fi

log_info ""
log_info "========================================="
log_info "  Omni-Core Middleware Installation abgeschlossen"
log_info "========================================="
log_info ""
log_info "Zugriffspunkte:"
log_info "  • API:         http://<container-ip>:8000"
log_info "  • Dokumentation: http://<container-ip>:8000/docs"
log_info "  • Gesundheitscheck: curl http://<container-ip>:8000/"
log_info "  • Systemd:     systemctl status omni-middleware.service"
log_info "  • Logs:        journalctl -u omni-middleware.service -f"
log_info ""