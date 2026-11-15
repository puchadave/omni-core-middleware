#!/bin/bash
# start.sh - Universal Start-Skript für Linux, macOS und Proxmox VE
#============================================================================
# Dieses Skript prüft, ob Docker und Docker Compose installiert sind,
# stoppt alte Instanzen, baut das Image und startet den Stack im Hintergrund.
#============================================================================

set -e

# Farben für Output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Omni-Core ERP & BI Enterprise Edition  ${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Prüfe, ob Docker installiert ist
if ! command -v docker &> /dev/null; then
    echo -e "${RED}✗ Docker ist nicht installiert.${NC}"
    echo "Bitte installieren Sie Docker zuerst."
    echo "https://docs.docker.com/engine/install/"
    exit 1
fi

# Prüfe, ob Docker Compose installiert ist
if ! command -v docker compose &> /dev/null; then
    echo -e "${RED}✗ Docker Compose ist nicht installiert.${NC}"
    echo "Bitte installieren Sie Docker Compose zuerst."
    exit 1
fi

echo -e "${YELLOW}🔍 Überprüfe bestehende Instanzen...${NC}"

# Stoppe alte Instanzen, falls vorhanden
if docker ps -a --filter "name=omni_db" --filter "name=omni_core" | grep -q -E "omni_db|omni_core"; then
    echo -e "${YELLOW}⏳ Stoppe bestehende Omni-Core-Instanzen...${NC}"
    docker stop omni_db omni_core 2>/dev/null || true
    docker rm omni_db omni_core 2>/dev/null || true
    echo -e "${GREEN}✓ Bestehende Container entfernt.${NC}"
fi

echo -e "${YELLOW}📦 Baume Omni-Core Docker-Image...${NC}"

# Baue das Image (nur wenn es nicht existiert)
if ! docker images -q omni-core-middleware:latest > /dev/null 2>&1; then
    echo -e "${YELLOW}   Erstmaliges Bauen des Images (duert ca. 2-3 Minuten)...${NC}"
    docker build -t omni-core-middleware . 2>&1 | while read line; do
        if echo "$line" | grep -qi "error\|failed\|FAILED"; then
            echo -e "${RED}   $line${NC}"
        else
            echo -e "   $line"
        fi
    done
    echo -e "${GREEN}✓ Image gebaut.${NC}"
fi

echo -e "${YELLOW}🚀 Starte Omni-Core Enterprise Stack im Hintergrund...${NC}"

# Starte den Stack im Hintergrund
docker compose up -d 2>&1

# Warten, bis die Datenbank bereit ist
echo -e "${YELLOW}⏳ Warte auf Datenbank-Bereitschaft (maximal 30 Sekunden)...${NC}"

for i in $(seq 1 30); do
    if docker exec omni_db pg_isready -U omnicore -d omnicore > /dev/null 2>&1; then
        echo -e "${GREEN}✓ Datenbank ist bereit!${NC}"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo -e "${RED}✗ Datenbank konnte nicht innerhalb von 30 Sekunden gestartet werden.${NC}"
        echo "Logs anzeigen: docker logs omni_db"
        exit 1
    fi
    sleep 1
done

# Warte kurz, damit die App fertig initialisiert ist
sleep 3

# Überprüfe, ob die Applikation läuft
if curl -s http://localhost:8000/api/health > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Omni-Core ist erreichbar unter http://localhost:8000${NC}"
else
    echo -e "${YELLOW}⚠ Omni-Core startet noch. Warte weitere 10 Sekunden...${NC}"
    sleep 10
    if curl -s http://localhost:8000/api/health > /dev/null 2>&1; then
        echo -e "${GREEN}✓ Omni-Core ist nun erreichbar!${NC}"
    else
        echo -e "${YELLOW}⚠ Prüfen Sie die Logs: docker logs omni_core${NC}"
    fi
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Omni-Core ist jetzt aktiv!${NC}"
echo -e "${GREEN}  Webinterface: http://localhost:8000/admin/products${NC}"
echo -e "${GREEN}  Health-Check:   http://localhost:8000/api/health${NC}"
echo -e "${GREEN}  Datenbank:      postgres://omnicore:omnicore_pass@localhost:5432/omnicore${NC}"
echo -e "${GREEN}========================================${NC}"