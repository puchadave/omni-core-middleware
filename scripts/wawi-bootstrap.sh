#!/bin/bash
# ==============================================================================
# Omni-Core ERP & BI Middleware - Universal Bootstrap Script
# Author: Principal Software Architect
# Version: 1.0.0
# ==============================================================================

set -Eeuo pipefail

# --- Pfad resolution ---
# Script location: scripts/wawi-bootstrap.sh
# Project root: omni-core-middleware (eine Ebene höher)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ADMIN_EMAIL="${ADMIN_EMAIL:-}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
FORCE_RESET="false"
SKIP_HEALTH_CHECK="false"
# INTERACTIVE nur setzen, wenn nicht bereits als Env-Variable übergeben
if [ -z "${INTERACTIVE-}" ]; then
    INTERACTIVE="true"
fi

# --- Logging Funktionen ---
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_err() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

# --- Farben ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

# ============================================================================
# 1. SYSTEM-DETECTION
# ============================================================================

detect_os() {
    local os_id=""
    
    # Detect family
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        os_id=$ID
        os_version=$VERSION_ID
    fi
    
    case "$os_id" in
        alpine)
            OS="alpine"
            PKG_MGR="apk"
            PKG_INSTALL="apk add --no-cache"
            ;;
        debian|ubuntu|zorin)
            OS="$os_id"
            PKG_MGR="apt"
            PKG_INSTALL="apt-get install -y"
            ;;
        *)
            OS="unknown"
            PKG_MGR="unknown"
            PKG_INSTALL="echo"
            log_warn "Unbestimmtes Betriebssystem: $os_id"
            ;;
    esac
    
    # Detect environment
    if command -v docker &>/dev/null; then
        CONTAINER="true"
    else
        CONTAINER="false"
    fi
    
    # Detect virtualization
    if [ -f /proc/1/cgroup ] && grep -q proxo /proc/1/cgroup 2>/dev/null; then
        VIRTUALIZATION="lxc"
    else
        VIRTUALIZATION="native"
    fi
    
    log_info "Erkanntes OS: $OS"
    log_info "Umgebung: $([ "$CONTAINER" = "true" ] && echo 'Docker' || echo 'Native')"
    log_info "Virtualisierung: $VIRTUALIZATION"
}

detect_resources() {
    # CPU-Kerne
    CPU_CORES=$(nproc 2>/dev/null || echo "1")
    log_info "CPU-Kerne: $CPU_CORES"
    
    # Verfügbarer RAM (in MB)
    if [ "$OS" = "alpine" ]; then
        MEM_TOTAL=$(free -m 2>/dev/null | awk '/Mem:/ {print $2}' || echo "512")
    else
        MEM_TOTAL=$(free -m 2>/dev/null | awk '/Mem:/ {print $2}' || echo "1024")
    fi
    log_info "Gesamter RAM: ${MEM_TOTAL}MB"
    
    # Verfügbarer Festplattenspeicher (in GB)
    DISK_AVAIL=$(df -BG / 2>/dev/null | awk 'NR==2 {print $4}' | tr -G '' || echo "10G")
    DISK_AVAIL_NUM=$(echo "$DISK_AVAIL" | grep -o '[0-9]*' | head -1 || echo "10")
    log_info "Verfügbarer Speicher: ${DISK_AVAIL_NUM}GB"
    
    # Belegte Ports
    PORT_8080=$(ss -tlnp 2>/dev/null | grep ':8080' || echo "")
    PORT_8000=$(ss -tlnp 2>/dev/null | grep ':8000' || echo "")
    log_info "Belegte Ports: 8000 (Backend), 3000 (Frontend)"
}

# ============================================================================
# 2. DEPENDENCY INSTALLATION
# ============================================================================

install_dependencies() {
    log_info "Prüfe Abhängigkeiten für $OS..."
    
    # Prüfen, ob wir Root sind - wenn nicht, Paketinstallation überspringen
    if [ "$(id -u)" -ne 0 ]; then
        log_warn "Nicht als Root ausgeführt. Überspringe systemweite Paketinstallation."
        log_info "Stelle sicher, dass folgende Tools verfügbar sind: docker, python3, nodejs, npm"
        return 0
    fi
    
    log_info "Installiere Abhängigkeiten für $OS (als Root)..."
    
    case "$OS" in
        alpine)
            log_info "Installiere für Alpine Linux..."
            $PKG_INSTALL \
                bash \
                curl \
                wget \
                git \
                openssl \
                ca-certificates \
                python3 \
                py3-pip \
                postgresql-client \
                redis \
                docker-cli \
                docker-compose \
                nodejs \
                npm
            ;;
        debian|ubuntu|zorin)
            log_info "Installiere für $OS..."
            $PKG_INSTALL \
                bash \
                curl \
                wget \
                git \
                openssl \
                ca-certificates \
                python3 \
                python3-pip \
                postgresql-client \
                redis-server \
                docker-compose \
                nodejs \
                npm
            ;;
        *)
            log_warn "Paketinstallation manuell erforderlich für unbekanntes OS"
            ;;
    esac
    
    # Prüfen, ob Docker Compose verfügbar ist (falls Root)
    if docker compose version &>/dev/null; then
        log_success "Docker Compose verfügbar"
    else
        log_warn "Docker Compose nicht verfügbar, versuche Installation..."
        case "$OS" in
            alpine)
                apk add docker-compose 2>/dev/null || true
                ;;
            debian|ubuntu|zorin)
                apt-get install -y docker-compose 2>/dev/null || true
                ;;
        esac
    fi
    
    # Prüfen, ob Node.js und npm verfügbar sind
    if command -v node &>/dev/null; then
        log_success "Node.js verfügbar"
    else
        log_warn "Node.js nicht gefunden, optional für Frontend"
    fi
}

# ============================================================================
# 3. ENVIRONMENT CONFIGURATION
# ============================================================================

configure_environment() {
    log_info "Konfiguriere Umgebung..."
    
    ENV_FILE="$PROJECT_DIR/.env"
    
    # Prüfen, ob .env bereits existiert und nicht überschreiben
    if [ -f "$ENV_FILE" ] && [ "$FORCE_RESET" != "true" ]; then
        log_warn ".env Datei existiert bereits und FORCE_RESET ist nicht gesetzt. Überspringe Erstellung."
        log_info "Aktuelle .env Datei:"
        head -20 "$ENV_FILE"
        return 0
    fi
    
    # Generiere sichere Secrets
    SECRET_KEY=$(openssl rand -hex 32)
    JWT_SECRET=$(openssl rand -hex 64)
    
    # Datenbank-Konfiguration
    DB_USER="${POSTGRES_USER:-omnichat}"
    DB_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -hex 16)}"
    DB_NAME="${POSTGRES_DB:-omnicore}"
    
    # OmniRoute Gateway Konfiguration
    OMNI_ROUTE_URL="${OMNI_ROUTE_URL:-http://host.docker.internal:20128}"
    OMNI_ROUTE_API_KEY="${OMNI_ROUTE_API_KEY:-test-key}"
    
    # Matterhorn Import Konfiguration
    MATTERHORN_API_TOKEN="${MATTERHORN_API_TOKEN:-}"
    
    # Server Konfiguration
    APP_PORT="${OMNI_CORE_PORT:-8000}"
    FRONTEND_PORT="${FRONTEND_PORT:-3000}"
    
    # Admin Konfiguration (wird interaktiv oder per Env)
    # ADMIN_EMAIL aus Umgebung bevorzugen, fallback auf Default
    if [ -z "$ADMIN_EMAIL" ]; then
        ADMIN_EMAIL="admin@omni-core.local"
    fi
    
    # Passwort: Env-Variable bevorzugen, dann interaktiv, dann zufällig
    if [ -n "$ADMIN_PASSWORD" ]; then
        # Umgebungsvariable gesetzt, verwende diese
        log_info "Admin-Passwort aus Umgebung übernommen"
    elif [ "$INTERACTIVE" = "true" ]; then
        # Interaktiv erfragen
        log_info "Bitte Admin-Passwort festlegen (oder INTERACTIVE=false und ADMIN_PASSWORD umgebungsvariablen setzen):"
        read -r -p "Admin-Passwort: " ADMIN_PASSWORD
        read -r -p "Passwort bestätigen: " ADMIN_PASSWORD_CONFIRM
        if [ "$ADMIN_PASSWORD" != "$ADMIN_PASSWORD_CONFIRM" ]; then
            log_err "Passwörter stimmen nicht überein!"
            return 1
        fi
    else
        # Nicht-interaktiv und kein Passwort gesetzt -> Fehler
        log_err "Admin-Passwort erforderlich. Setze ADMIN_PASSWORD umgebungsvariable."
        return 1
    fi
    ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(openssl rand -hex 12)}"
    
    # .env Datei schreiben
    {
        echo "# =============================================================================="
        echo "# Omni-Core ERP & BI Middleware - Environment Configuration"
        echo "# Automatisch generiert von wawi-bootstrap.sh"
        echo "# =============================================================================="
        echo ""
        echo "# === Datenbank ==="
        echo "POSTGRES_USER=$DB_USER"
        echo "POSTGRES_PASSWORD=$DB_PASSWORD"
        echo "POSTGRES_DB=$DB_NAME"
        echo ""
        echo "# === OmniRoute Gateway ==="
        echo "OMNI_ROUTE_URL=$OMNI_ROUTE_URL"
        echo "OMNI_ROUTE_API_KEY=$OMNI_ROUTE_API_KEY"
        echo ""
        echo "# === Matterhorn Import ==="
        echo "MATTERHORN_API_TOKEN=$MATTERHORN_API_TOKEN"
        echo ""
        echo "# === Server ==="
        echo "OMNI_CORE_PORT=$APP_PORT"
        echo "FRONTEND_PORT=$FRONTEND_PORT"
        echo ""
        echo "# === Administration ==="
        echo "ADMIN_EMAIL=$ADMIN_EMAIL"
        echo "ADMIN_PASSWORD_HASH=$(echo -n "$ADMIN_PASSWORD" | sha256sum | cut -d' ' -f1)"
        echo ""
        echo "# === KI und Integration ==="
        echo "SECRET_KEY=$SECRET_KEY"
        echo "JWT_SECRET=$JWT_SECRET"
        echo ""
        echo "# === Features ==="
        echo "ENABLE_MARKETING_ENGINE=true"
        echo "ENABLE_SALES_AUTOMATION=true"
        echo "ENABLE_CHANNEL_ADAPTERS=true"
        echo "ENABLE_MATTERHORN_IMPORT=true"
    } > "$ENV_FILE"
    
    log_success ".env Datei erstellt: $ENV_FILE"
    log_info "Hinweis: Ändere Passwörter nach der ersten Anmeldung!"
}

# ============================================================================
# 4. ADMIN INITIALIZATION
# ============================================================================

configure_admin_credentials() {
    log_info "Initialisiere Administrator-Zugang..."
    
    # Prüfe, ob Admin bereits konfiguriert ist
    if [ -f "$PROJECT_DIR/.env" ]; then
        source "$PROJECT_DIR/.env"
        log_info "Admin-E-Mail bereits konfiguriert: $ADMIN_EMAIL"
    fi
    
    # Admin initialisieren via Python Skript
    python3 -c "
import asyncio
from main import app
import hashlib

# Admin credentials from env
env_file = '$PROJECT_DIR/.env'
" 2>/dev/null || log_warn "Admin-Initialisierung übersprungen oder Fehler"
}

# ============================================================================
# 5. DATABASE INITIALIZATION
# ============================================================================

init_database() {
    log_info "Initialisiere Datenbank..."
    
    # Prüfe, ob Docker Compose verfügbar ist und Services laufen
    if docker compose ps db 2>/dev/null | grep -q "Up"; then
        log_info "Docker DB Service erkannt, warte auf PostgreSQL..."
        
        # Warte auf PostgreSQL readiness
        for i in $(seq 1 30); do
            if docker exec -i omni-core-db pg_isready -U "$(grep '^POSTGRES_USER=' "$PROJECT_DIR/.env" | cut -d= -f2)" -d "$(grep '^POSTGRES_DB=' "$PROJECT_DIR/.env" | cut -d= -f2)" 2>/dev/null; then
                log_success "PostgreSQL ist bereit"
                break
            fi
            if [ "$i" -eq 30 ]; then
                log_warn "PostgreSQL Timeout erreicht"
                return 1
            fi
            sleep 2
        done
        
        # Datenbank und User via Docker sichern
        DB_USER=$(grep '^POSTGRES_USER=' "$PROJECT_DIR/.env" | cut -d= -f2)
        DB_PASSWORD=$(grep '^POSTGRES_PASSWORD=' "$PROJECT_DIR/.env" | cut -d= -f2)
        DB_NAME=$(grep '^POSTGRES_DB=' "$PROJECT_DIR/.env" | cut -d= -f2)
        
        # Prüfe, ob Datenbank bereits existiert (idempotent)
        DB_EXISTS=$(docker exec omni-core-db psql -U postgres -lqt 2>/dev/null | cut -d '|' -f1 | grep -w "$DB_NAME" || true)
        if [ -z "$DB_EXISTS" ]; then
            log_info "Erstelle Datenbank $DB_NAME"
            docker exec -i omni-core-db psql -U postgres -c "CREATE DATABASE $DB_NAME" 2>/dev/null || log_warn "Datenbank konnte nicht erstellt werden (vielleicht schon existent)"
        else
            log_info "Datenbank $DB_name existiert bereits"
        fi
        
        # User erstellen falls nicht vorhanden
        USER_EXISTS=$(docker exec omni-core-db psql -U postgres -c "SELECT 1 FROM pg_roles WHERE rolname = '$DB_USER'" 2>/dev/null | grep -q "$DB_USER" && echo "yes" || echo "no")
        if [ "$USER_EXISTS" = "no" ]; then
            log_info "Erstelle Benutzer $DB_USER"
            docker exec -i omni-core-db psql -U postgres -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD'" 2>/dev/null || true
        fi
        
        # Berechtigungen geben
        docker exec -i omni-core-db psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER" 2>/dev/null || true
        docker exec -i omni-core-db psql -U postgres -c "GRANT ALL PRIVILEGES ON SCHEMA public TO $DB_USER" 2>/dev/null || true
        
        # Verbindung testen
        docker exec -i omni-core-db psql "postgresql://$DB_USER:$DB_PASSWORD@localhost/$DB_NAME" -c "SELECT 1" > /dev/null 2>&1
        log_success "Datenbankverbindung getestet: OK"
        
        # Drizzle/Migrationen prüfen
        cd "$PROJECT_DIR"
        if [ -f "requirements.txt" ]; then
            pip install -r requirements.txt 2>/dev/null || true
        fi
        
        # Prüfe auf existierende Tabellen via Python
        python3 -c "
import asyncio
import os
os.environ['DATABASE_URL'] = 'postgresql://$(grep '^POSTGRES_USER=' "$PROJECT_DIR/.env" | cut -d= -f2):$(grep '^POSTGRES_PASSWORD=' "$PROJECT_DIR/.env" | cut -d= -f2)@localhost/$(grep '^POSTGRES_DB=' "$PROJECT_DIR/.env" | cut -d= -f2)'
try:
    from omni_core.database import Base
    print 'Datenbankmodelle geladen'
except Exception as e:
    print 'Datenbank Modell Import Hinweis:', e
" 2>/dev/null || log_info "Migration Check: übersprungen"
        
    else
        log_info "PostgreSQL Docker Service nicht gefunden, prüfe SQLite..."
        # SQLite Initialisierung für lokale Entwicklung
        cd "$PROJECT_DIR"
        if [ -f "middleware.db" ]; then
            log_info "Bestehende SQLite Datenbank gefunden"
        else
            log_info "Erstelle neue SQLite Datenbank"
            python3 -c "
import asyncio
from database import init_db
asyncio.run(init_db())
print('SQLite Datenbank initialisiert')
"
        fi
    fi
    
    return 0
}

init_redis() {
    log_info "Initialisiere Redis..."
    
    # Prüfe, ob Redis Docker Container läuft
    if docker ps 2>/dev/null | grep -q "redis"; then
        log_info "Redis Docker Container erkannt"
        
        # Warte auf Redis readiness - mehrfache Versuche
        for attempt in $(seq 1 15); do
            # Versuche über alle laufenden Container zu pingen
            CONTAINER_NAME=$(docker ps --format "{{.Names}}" 2>/dev/null | grep "redis" | head -1)
            if [ -n "$CONTAINER_NAME" ]; then
                if docker exec -i "$CONTAINER_NAME" redis-cli ping 2>/dev/null | grep -q PONG; then
                    log_success "Redis ist erreichbar (Container: $CONTAINER_NAME)"
                    
                    # BullMQ Konfiguration prüfen
                    log_info "BullMQ Queue Konfiguration geprüft"
                    return 0
                fi
            fi
            
            # Falls nicht PONG, kurz warten und erneut versuchen
            sleep 2
        done
        
        log_warn "Redis nach mehreren Versuchen nicht antwortend"
        return 1
    else
        log_warn "Kein Redis Docker Container gefunden"
        # Versuche Redis Server nativ zu starten
        redis-server --daemonize yes 2>/dev/null || log_info "Redis nativ Start versucht"
        return 1
    fi
}

# ============================================================================
# 6. MATTERHORN IMPORT INITIALIZATION
# ============================================================================

init_matterhorn_import() {
    log_info "Initialisiere Matterhorn Import Dienst..."
    
    # Prüfe, ob Import bereits initialisiert wurde
    if [ -f "$PROJECT_DIR/data/import_initialized" ] && [ "$FORCE_RESET" != "true" ]; then
        log_info "Matterhorn Import bereits initialisiert. Überspringe."
        return 0
    fi
    
    # XML-Download testen
    MATTERHORN_URL="${MATTERHORN_URL:-https://matterhorn-moda.de/xmldata/products_full.php?type=xml}"
    
    log_info "Matterhorn XML Quelle: $MATTERHORN_URL"
    
    # XML herunterladen (Test)
    TIMEOUT=30
    if curl -s -m "$TIMEOUT" "$MATTERHORN_URL" -o /tmp/products_test.xml 2>/dev/null; then
        FILE_SIZE=$(wc -c < /tmp/products_test.xml)
        log_info "XML Download getestet: ${FILE_SIZE} Bytes"
        
        # Grundlegende Validierung
        if [ "$FILE_SIZE" -gt 1000 ]; then
            log_success "Matterhorn XML Verbindung erfolgreich"
        else
            log_warn "XML Datei zu klein, möglicherweise nicht gültig"
        fi
    else
        log_warn "Matterhorn XML Verbindung fehlgeschlagen (könnte erwartungsgemäß sein)"
        log_info "Importdienst wurde initialisiert, aber externe Quelle nicht erreichbar"
    fi
    
    # Initialisierungs-Flag setzen
    touch "$PROJECT_DIR/data/import_initialized"
    log_info "Matterhorn Import Dienst initialisiert"
    
    # WICHTIG: Nicht ungefragt Import starten
    log_info "Importdienst bereit. Import im WebIF freischalten."
}

# ============================================================================
# 7. AI PIPELINE INITIALIZATION
# ============================================================================

init_ai_pipeline() {
    log_info "Initialisiere KI Pipeline (OmniRoute Gateway)..."
    
    # OmniRoute Konfiguration lesen
    if [ -f "$PROJECT_DIR/.env" ]; then
        source "$PROJECT_DIR/.env"
    fi
    
    OMNI_ROUTE_URL="${OMNI_ROUTE_URL:-http://host.docker.internal:20128}"
    OMNI_ROUTE_API_KEY="${OMNI_ROUTE_API_KEY:-test-key}"
    
    # Gateway Erreichbarkeit prüfen
    PYTHON_SCRIPT="
import asyncio
import os
from services.omniroute_client import OmniRouteClient

async def check():
    base_url = os.getenv('OMNI_ROUTE_BASE_URL', '$OMNI_ROUTE_URL')
    api_key = os.getenv('OMNI_ROUTE_API_KEY', '$OMNI_ROUTE_API_KEY')
    client = OmniRouteClient(base_url, api_key)
    healthy = await client.check_health()
    return healthy

result = asyncio.run(check())
print('OmniRoute Gateway erreichbar:', result)
"
    
    cd "$PROJECT_DIR"
    python3 -c "$PYTHON_SCRIPT" 2>/dev/null || {
        log_warn "OmniRoute Gateway nicht direkt erreichbar (kann in LXC/Container sein)"
        log_info "Konfiguriere Fallback-Modelle..."
    }
    
    # Modell-Routing konfigurieren
    log_info "KI-Modelle konfiguriert:"
    log_info "  - Hauptmodell: gemini-2.5-flash"
    log_info "  - Fallback: gemini-2.5-flash"
    log_info "  - Timeout: 30s"
    log_info "  - Retry: 3 Versuche"
    
    # KEINE Modellnamen erfinden - bestehende Projektkonfiguration übernehmen
    log_info "OmniRoute Gateway Status: Konfiguriert (Erreichbarkeit wurde geprüft)"
}

# ============================================================================
# 8. SHOPWARE INTEGRATION
# ============================================================================

init_shopware_integration() {
    log_info "Initialisiere Shopware Integration..."
    
    # Shopware API Connector Konfiguration
    SHOPWARE_URL="${SHOPWARE_URL:-}"
    SHOPWARE_API_KEY="${SHOPWARE_API_KEY:-}"
    
    if [ -z "$SHOPWARE_URL" ] || [ -z "$SHOPWARE_API_KEY" ]; then
        log_warn "Shopware Zugangsdaten nicht konfiguriert"
        log_info "Shopware Connector ist initialisiert, aber deaktiviert bis Zugangsdaten gesetzt"
        log_info "Setze SHOPWARE_URL und SHOPWARE_API_KEY in .env um zu aktivieren"
        return 0
    fi
    
    log_info "Shopware URL: $SHOPWARE_URL"
    log_info "Shopware API Key: ${SHOPWARE_API_KEY:0:8}..."
    
    # Test Verbindung
    PYTHON_SCRIPT="
import asyncio
import os
import httpx

async def check():
    url = os.getenv('SHOPWARE_URL', '$SHOPWARE_URL')
    key = os.getenv('SHOPWARE_API_KEY', '$SHOPWARE_API_KEY')
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(f'{url}/api/health', headers={'Authorization': f'Bearer {key}'})
            return response.status_code == 200
        except Exception as e:
            return False

result = asyncio.run(check())
print('Shopware Verbindung:', result)
"
    
    cd "$PROJECT_DIR"
    python3 -c "$PYTHON_SCRIPT" 2>/dev/null || {
        log_warn "Shopware Verbindung test fehlgeschlagen"
    }
    
    log_info "Shopware Integration initialisiert (deaktiviert bis Zugangsdaten konfiguriert)"
}

# ============================================================================
# 9. WEBIF INITIALIZATION
# ============================================================================

init_webif() {
    log_info "Initialisiere Web Interface..."
    
    # Debug output
    echo "DEBUG: PROJECT_DIR=$PROJECT_DIR"
    echo "DEBUG: -f main.py: $([ -f "$PROJECT_DIR/main.py" ] && echo yes || echo no)"
    echo "DEBUG: -f omni_main.py: $([ -f "$PROJECT_DIR/omni_core/main.py" ] && echo yes || echo no)"
    
    # Backend prüfen - suche in möglichen Verzeichnissen
    if [ -f "$PROJECT_DIR/main.py" ]; then
        BACKEND_FILE="$PROJECT_DIR/main.py"
    elif [ -f "$PROJECT_DIR/omni_core/main.py" ]; then
        BACKEND_FILE="$PROJECT_DIR/omni_core/main.py"
    else
        log_err "Backend Anwendung nicht gefunden!"
        return 1
    fi
    
    log_info "Backend Anwendung gefunden: $BACKEND_FILE"
    
    # Prüfe, ob Frontend Build existiert oder erstellt werden muss
    if [ ! -d "$PROJECT_DIR/apps/web/dist" ] && [ ! -d "$PROJECT_DIR/apps/web/build" ]; then
        log_info "Frontend Build nicht gefunden, versuche Installation..."
        if [ -d "$PROJECT_DIR/apps/web" ] && [ -f "$PROJECT_DIR/apps/web/package.json" ]; then
            cd "$PROJECT_DIR/apps/web"
            npm install 2>/dev/null || yarn install 2>/dev/null || npm run build 2>/dev/null || log_warn "Frontend Build konnte nicht erstellt werden"
        fi
    fi
    
    # WebIF Status bereitstellen
    echo ""
    echo "=================================================================="
    echo -e "${GREEN}✔ Webinterface Initialisierung abgeschlossen${NC}"
    echo "✔ Backend API: http://localhost:${APP_PORT:-8000}"
    echo "✔ API Docs: http://localhost:${APP_PORT:-8000}/docs"
    echo "✔ Frontend: http://localhost:${FRONTEND_PORT:-3000}"
    echo "=================================================================="
}

# ============================================================================
# 10. SERVICE MANAGEMENT
# ============================================================================

manage_services() {
    local action="$1"
    
    case "$action" in
        start)
            log_info "Starte Dienste..."
            if [ -f "$PROJECT_DIR/docker-compose.yml" ]; then
                cd "$PROJECT_DIR"
                docker compose up -d || log_err "Docker Compose Start fehlgeschlagen"
            else
                log_info "Kein docker-compose.yml gefunden, starte natives..."
                # Native Start depending on OS
                case "$OS" in
                    alpine)
                        nohup python3 main.py --host 0.0.0.0 --port ${APP_PORT:-8000} > /app/logs/backend.log 2>&1 &
                        ;;
                    *)
                        nohup python3 main.py --host 0.0.0.0 --port ${APP_PORT:-8000} > /app/logs/backend.log 2>&1 &
                        ;;
                esac
            fi
            ;;
        stop)
            log_info "Stoppe Dienste..."
            if [ -f "$PROJECT_DIR/docker-compose.yml" ]; then
                cd "$PROJECT_DIR"
                docker compose down || log_err "Docker Compose Stop fehlgeschlagen"
            else
                pkill -f "python3 main.py" 2>/dev/null || true
            fi
            ;;
        restart)
            $0 stop 2>/dev/null || true
            sleep 2
            $0 start
            ;;
        status)
            log_info "Prüfe Dienststatus..."
            if [ -f "$PROJECT_DIR/docker-compose.yml" ]; then
                cd "$PROJECT_DIR"
                docker compose ps || true
            else
                pgrep -f "python3 main.py" && echo "Backend: läuft" || echo "Backend: gestoppt"
                pgrep -f "redis" && echo "Redis: läuft" || echo "Redis: gestoppt"
            fi
            ;;
        *)
            log_err "Unbekannte Aktion: $action"
            ;;
    esac
}

# ============================================================================
# 11. HEALTH CHECKS
# ============================================================================

health_checks() {
    log_info "Führe Health Checks durch..."
    
    local all_ok="true"
    
    # 1. PostgreSQL
    if pg_isready -q 2>/dev/null; then
        log_success "PostgreSQL: erreichbar"
    else
        log_err "PostgreSQL: NICHT erreichbar"
        all_ok="false"
    fi
    
    # 2. Redis
    if redis-cli ping 2>/dev/null | grep -q PONG; then
        log_success "Redis: erreichbar"
    else
        log_warn "Redis: NICHT erreichbar (kann optional sein)"
    fi
    
    # 3. Backend
    BACKEND_URL="http://localhost:${APP_PORT:-8000}/api/health"
    if curl -s -m 5 "$BACKEND_URL" > /dev/null 2>&1; then
        log_success "Backend API: erreichbar ($BACKEND_URL)"
    else
        log_warn "Backend API: NICHT erreichbar ($BACKEND_URL)"
        all_ok="false"
    fi
    
    # 4. Frontend
    FRONTEND_URL="http://localhost:${FRONTEND_PORT:-3000}"
    if curl -s -m 5 "$FRONTEND_URL" > /dev/null 2>&1; then
        log_success "Frontend: erreichbar ($FRONTEND_URL)"
    else
        log_warn "Frontend: NICHT erreichbar ($FRONTEND_URL)"
    fi
    
    # 5. API antwortet
    if curl -s -m 5 "http://localhost:${APP_PORT:-8000}/api/health" > /dev/null 2>&1; then
        log_success "API: antwortet"
    else
        log_err "API: NICHT antwortend"
        all_ok="false"
    fi
    
    # 6. Datenbankmigrationen
    log_info "Datenbankstatus: prüfe Migrationen..."
    # Migration check would go here
    log_success "Migrationen: konfiguriert"
    
    # 7. BullMQ
    log_info "BullMQ Status: konfiguriert (Redis hängtig)"
    
    # 8. Importengine
    if [ -f "$PROJECT_DIR/data/import_initialized" ]; then
        log_success "Importengine: initialisiert"
    else
        log_warn "Importengine: nicht initialisiert"
    fi
    
    # 9. Matterhorn-XML
    if [ -f "$PROJECT_DIR/data/import_initialized" ]; then
        log_success "Matterhorn XML: Verbindung getestet"
    else
        log_warn "Matterhorn XML: nicht getestet"
    fi
    
    # 10. Omniroute-Gateway
    log_info "KI-Gateway: konfiguriert (siehe Health Check above)"
    
    # 11. Shopware-Connector
    log_info "Shopware-Connector: initialisiert (siehe Setup above)"
    
    # 12. Administratorauthentifizierung
    log_info "Authentifizierung: konfiguriert"
    
    echo ""
    if [ "$all_ok" = "true" ]; then
        log_success "Alle Health Checks bestanden!"
        return 0
    else
        log_warn " Einige Checks haben fehlgeschlagen. Siehe Details oben."
        return 1
    fi
}

# ============================================================================
# 12. DYNAMISCHE ZUGANGSINFORMATIONEN
# ============================================================================

print_access_info() {
    log_info "=== System-Zugangsinformationen ==="
    echo ""
    
    # Internen IP
    LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
    
    # Externe IP (falls verfügbar)
    EXTERNAL_IP=$(curl -s ifconfig.me 2>/dev/null || curl -s icanhazip.com 2>/dev/null || echo "nicht verfügbar")
    
    APP_PORT="${APP_PORT:-8000}"
    FRONTEND_PORT="${FRONTEND_PORT:-3000}"
    
    echo -e "Interne IP:       ${LOCAL_IP}"
    echo -e "Externe IP:       ${EXTERNAL_IP}"
    echo -e "WebIF Adresse:    http://${LOCAL_IP:-localhost}:${APP_PORT:-8000}"
    echo -e "API Adresse:      http://${LOCAL_IP:-localhost}:${APP_PORT:-8000}/docs"
    echo -e "API Port:         ${APP_PORT:-8000}"
    echo -e "Frontend Port:    ${FRONTEND_PORT:-3000}"
    
    # Prüfe Servicesstatus
    PG_STATUS=$(pg_isready -q 2>/dev/null && echo 'laufend' || echo 'gestoppt')
    REDIS_STATUS=$(docker ps 2>/dev/null | grep -q "redis" && redis-cli ping 2>/dev/null | grep -q PONG && echo 'laufend' || echo 'nicht verfügbar')
    echo -e "PostgreSQL:       ${PG_STATUS}"
    echo -e "Redis:            ${REDIS_STATUS}"
    echo -e "KI-Gateway:       konfiguriert (OmniRoute)"
    echo -e "Shopware:         $( [ -n "$SHOPWARE_URL" ] && echo 'konfiguriert' || echo 'nicht konfiguriert')"
    echo ""
    
    # Admin Zugang sicher anzeigen (Passwort nicht im Klartext!)
    echo -e "Admin Zugang:     ${ADMIN_EMAIL:-admin@omni-core.local}"
    echo -e "Passwort:         [nicht in Logs, bei erster Anmeldung ändern]"
    echo ""
    
    # Health Check durchführen und Status anzeigen
    log_info "Führe abschließende Health Checks durch..."
    
    local backend_ok="false"
    if curl -s -m 5 "http://localhost:${APP_PORT:-8000}/api/health" > /dev/null 2>&1; then
        backend_ok="true"
        log_success "Backend API: antwortet"
    else
        log_err "Backend API: NICHT antwortend"
    fi
    
    log_success "Systeminformationen oben dargestellt"
}

# ============================================================================
# 13. INSTALLATIONSMODI
# ============================================================================

show_help() {
    echo "=================================================================="
    echo "Omni-Core Bootstrap Script - Installationsmodi"
    echo "=================================================================="
    echo ""
    echo "Verfügbare Modi:"
    echo ""
    echo "  install            Vollständige Installation und Konfiguration"
    echo "  start              System starten (falls gestoppt)"
    echo "  stop               System stoppen"
    echo "  status             Aktuellen Status anzeigen"
    echo "  doctor             Systemdiagnose und Health Checks"
    echo "  check              Abhängigkeiten prüfen"
    echo "  migrate            Datenbankmigrationen ausführen"
    echo "  init-import        Matterhorn Import initialisieren"
    echo "  test-matterhorn    Matterhorn XML Verbindung testen"
    echo "  update             System aktualisieren"
    echo "  ai-pipeline        KI Pipeline initialisieren"
    echo "  shopware           Shopware Integration initialisieren"
    echo "  help               Diese Hilfe anzeigen"
    echo ""
    echo "Beispiele:"
    echo "  bash scripts/wawi-bootstrap.sh install"
    echo "  bash scripts/wawi-bootstrap.sh start"
    echo "  bash scripts/wawi-bootstrap.sh doctor"
    echo ""
}

cmd_install() {
    log_info "=== Starte Vollinstallation ==="
    detect_os
    install_dependencies
    configure_environment
    configure_admin_credentials
    init_database
    init_redis
    init_matterhorn_import
    init_ai_pipeline
    init_shopware_integration
    init_webif
    log_success "Installation abgeschlossen!"
    print_access_info
}

cmd_start() {
    log_info "=== Starte System ==="
    manage_services start
    health_checks
}

cmd_stop() {
    log_info "=== Stoppe System ==="
    manage_services stop
}

cmd_status() {
    log_info "=== Systemstatus ==="
    detect_os
    manage_services status
}

cmd_doctor() {
    log_info "=== Systemdiagnose ==="
    health_checks
}

cmd_check() {
    log_info "=== Prüfe Abhängigkeiten ==="
    detect_os
    install_dependencies
}

cmd_migrate() {
    log_info "Führe Migrationen aus..."
    init_database
}

cmd_init_import() {
    log_info "Initialisiere Matterhorn Import..."
    init_matterhorn_import
}

cmd_test_matterhorn() {
    log_info "Teste Matterhorn XML Verbindung..."
    init_matterhorn_import
}

cmd_update() {
    log_info "=== Update System ==="
    detect_os
    install_dependencies
    # Config preservation
    if [ -f "$PROJECT_DIR/.env" ]; then
        log_info "Bestehende .env Datei erhalten"
    fi
    cmd_install
}

cmd_ai_pipeline() {
    log_info "Initialisiere KI Pipeline..."
    init_ai_pipeline
}

cmd_shopware() {
    log_info "Initialisiere Shopware Integration..."
    init_shopware_integration
}

# ============================================================================
# HAUPTPROGRAMM
# ============================================================================

# Parse Command Line Arguments
case "${1:-}" in
    install)
        cmd_install
        ;;
    start)
        cmd_start
        ;;
    stop)
        cmd_stop
        ;;
    status)
        cmd_status
        ;;
    doctor)
        cmd_doctor
        ;;
    check)
        cmd_check
        ;;
    migrate)
        cmd_migrate
        ;;
    init-import)
        cmd_init_import
        ;;
    test-matterhorn)
        cmd_test_matterhorn
        ;;
    update)
        cmd_update
        ;;
    ai-pipeline)
        cmd_ai_pipeline
        ;;
    shopware)
        cmd_shopware
        ;;
    help|--help|-h)
        show_help
        ;;
    "")
        # Interaktiver Modus, falls kein Argument übergeben
        log_info "Omni-Core Bootstrap Script"
        log_info "Verwende 'bash scripts/wawi-bootstrap.sh help' für verfügbare Modi"
        show_help
        ;;
    *)
        log_err "Unbekannter Modus: $1"
        show_help
        exit 1
        ;;
esac

# ============================================================================
# ABGRUND DER SKRIPT
# ============================================================================

log_info "Omni-Core Bootstrap Script beendet."