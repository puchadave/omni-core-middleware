# ==============================================================================
# Omni-Core ERP & BI Middleware - Proxmox VE One-Click Installer
# ==============================================================================

# Autor: puchadave <github.com/puchadave>
# Repository: https://github.com/puchadave/omni-core-middleware
# Version: 1.0.0

# DIESE INSTALLATIONSDATEIEN DIREKT VOM GITHUB-REPOSITORY LADEN:

# Einzelne Dateien direkt herunterladen und ausführen:

# Basis-Installation (Proxmox Host):
# bash -c "$(curl -fsSL https://raw.githubusercontent.com/puchadave/omni-core-middleware/main/deploy/proxmox/install.sh)"

# Vollständiger Ablauf (alle Schritte):
# 1. Installer:    bash -c "$(curl -fsSL https://raw.githubusercontent.com/puchadave/omni-core-middleware/main/deploy/proxmox/install.sh)"
# 2. Bootstrap:    Nach der Installation das Script scripts/wawi-bootstrap.sh im Container ausführen
#                  oder: bash -c "$(curl -fsSL https://raw.githubusercontent.com/puchadave/omni-core-middleware/main/deploy/proxmox/bootstrap.sh)"
# 3. Healthcheck:  bash -c "$(curl -fsSL https://raw.githubusercontent.com/puchadave/omni-core-middleware/main/deploy/proxmox/healthcheck.sh) <CTID>"
# 4. Update:       bash scripts/update.sh <CTID>
# 5. Uninstall:    bash -c "$(curl -fsSL https://raw.githubusercontent.com/puchadave/omni-core-middleware/main/deploy/proxmox/uninstall.sh)"

# ==============================================================================
# INSTALLATIONSABLAUF
# ==============================================================================

# Der Installer führt folgende Schritte automatisch aus:

# [01/15] Proxmox erkannt
#   - Proxmox VE Version wird erkannt
#   - Systemressourcen (CPU, RAM, Storage) werden geprüft

# [02/15] Ressourcen geprüft
#   - Ausreichender Arbeitsspeicher (mind. 4 GB empfohlen)
#   - Genügend freier Speicher (mind. 32 GB empfohlen)
#   - Verfügbare LXC Container-Slots

# [03/15] Storage geprüft
#   - Verfügbare Storage Pools (local-lvm, local, ZFS)
#   - Passenden Storage für Container auswählen

# [04/15] LXC-Template geladen
#   - Debian 13 Template herunterladen
#   - Falls nicht vorhanden: automatischer Download aus Proxmox Repository

# [05/15] Container erstellt
#   - Container-ID (CTID) automatisch oder manuell vergeben
#   - Hostname: standardmäßig "wawi"
#   - CPU-Kern-Zuweisung (Standard: 4 Kerne)
#   - RAM-Zuweisung (Standard: 4096 MB / 4 GB)
#   - Disk-Größe (Standard: 32 GB)
#   - Storage Pool Auswahl
#   - Netzwerkbrücke (Standard: vmbr0, DHCP)

# [06/15] Netzwerk konfiguriert
#   - Bridge-Einrichtung (vmbr0)
#   - IP-Adresse: DHCP (standardmäßig) oder statisch konfigurierbar
#   - DNS-Einstellungen (8.8.8.8, 8.8.4.4)
#   - Gateway-Konfiguration

# [07/15] Container gestartet
#   - System startet den Container
#   - Wartezeit auf Boot-Fertigstellung (max. 60 Sekunden)

# [08/15] Betriebssystem eingerichtet
#   - Paket-Quellen aktualisieren (apt-get update / apk update)
#   - Essentielle Pakete installieren (bash, curl, wget, git, openssl)
#   - PostgreSQL und Redis Datenbanken konfigurieren
#   - System-Updates anwenden

# [09/15] Abhängigkeiten installiert
#   - Git klonen oder aktualisieren
#   - Node.js und pnpm/ npm installieren
#   - Python/uvicorn für FastAPI backend
#   - Bau-Abhängigkeiten installieren
#   - Existierende Lockfiles (pnpm-lock.yaml, package-lock.json) bevorzugen

# [10/15] PostgreSQL eingerichtet
#   - PostgreSQL Datenbank starten
#   - Datenbank "omnicore" erstellen falls nicht vorhanden
#   - Benutzer "omnichat" mit sicheren Passwort erstellen
#   - PostgreSQL Parameter an RAM-Anpassung anpassen
#   - Migrationen prüfen und ausführen

# [11/15] Redis eingerichtet
#   - Redis Server starten
#   - Redis Tools und BullMQ Vorbereitung
#   - Cache-Konfiguration prüfen

# [12/15] WaWi installiert
#   - Repository in /wawi oder $INSTALL_DIR klonen
#   - Existierende Projektkonfigurationen respektieren
#   - .env Datei erstellen oder aktualisieren
#   - Produktionsfähige Konfiguration zusammenstellen

# [13/15] Datenbank initialisiert
#   - Datenbank-Schema prüfen und migrieren
#   - Drizzle ORM / SQLAlchemy Modelle prüfen
#   - Seed-Daten bei Bedarf erzeugen (nicht überschreiben)
#   - Verbindung testen

# [14/15] Dienste gestartet
#   - Backend API (uvicorn, Port 8000) starten
#   - Frontend Production Server starten (falls verfügbar)
#   - Worker (BullMQ Import/KI Processing) starten
#   - Autostart für Container und Dienste konfigurieren

# [15/15] Healthchecks abgeschlossen
#   - LXC Container Status prüfen
#   - Netzwerk Konnektivität testen
#   - PostgreSQL Datenbank prüfen
#   - Redis Cache prüfen
#   - Backend API Health Endpoint testen
#   - Frontend Oberfläche testen
#   - Worker / BullMQ Status prüfen
#   - Matterhorn Import Verbindung testen
#   - OmniRoute Gateway Konfiguration prüfen
#   - Shopware Connector Status prüfen
#   - Administrator Login testen

# ==============================================================================
# NACH DER INSTALLATION
# ==============================================================================

# Nach erfolgreichem Abschluss (Status: PRODUCTION READY) ist folgende Adressen erreichbar:

# Web Interface (WebIF):
#   http://<CONTAINER-IP>:8000
#   - Administrator Login einrichten
#   - Produktimport verwalten
#   - KI Einstellungen konfigurieren
#   - Shopware Konfiguration
#   - Matterhorn Import steuern

# API Documentation:
#   http://<CONTAINER-IP>:8000/docs
#   - FastAPI automatische Dokumentation
#   - Alle verfügbaren Endpunkte auflisten

# System Status:
#   - PRODUCTION READY (nach erfolgreicher Healthcheck-Phase)
# - PostgreSQL: ONLINE
# - Redis: ONLINE
# - Backend: ONLINE
# - Frontend: ONLINE
# - Worker: ONLINE

# Integrationsstatus:
# - Matterhorn: READY (Import initialisiert, aber nicht automatisch gestartet)
# - Omniroute: CONFIGURATION STATUS (API Key konfigurieren falls nötig)
# - Shopware: CONFIGURATION STATUS (URL und API Key bei Bedarf eintragen)

# ==============================================================================
# EINZELNE KOMMANDOS
# ==============================================================================

# Installationsbefehl (einzeilig):
# bash -c "$(curl -fsSL https://raw.githubusercontent.com/puchadave/omni-core-middleware/main/deploy/proxmox/install.sh)"

# Nicht-interaktiv mit vorkonfigurierten Werten:
# WAWI_CTID=120 WAWI_HOSTNAME=wawi WAWI_CORES=4 WAWI_MEMORY=4096 \
# WAWI_DISK=32 WAWI_STORAGE=local-lvm bash -c "$(curl -fsSL https://raw.githubusercontent.com/puchadave/omni-core-middleware/main/deploy/proxmox/install.sh)"

# Bootstrap nachträglich ausführen:
# bash -c "$(curl -fsSL https://raw.githubusercontent.com/puchadave/omni-core-middleware/main/deploy/proxmox/bootstrap.sh)"

# Healthcheck nach Installation:
# bash scripts/healthcheck.sh <CTID> (vom Host aus) oder
# bash -c "$(curl -fsSL https://raw.githubusercontent.com/puchadave/omni-core-middleware/main/deploy/proxmox/healthcheck.sh) <CTID>"

# System aktualisieren:
# bash scripts/update.sh <CTID>

# Container deinstallieren:
# bash -c "$(curl -fsSL https://raw.githubusercontent.com/puchadave/omni-core-middleware/main/deploy/proxmox/uninstall.sh)"

# ==============================================================================
# VORAUSSETZUNGEN
# ==============================================================================

# Proxmox VE Version 8.x oder neuer empfohlen
# Root-Rechte auf dem Proxmox Host
# Mindestens 8 GB RAM auf dem Host (4 GB für Container, Rest für Host)
# Mindestens 50 GB freier Speicher auf dem Host
# Internetverbindung für GitHub und Package Downloads
# curl oder wget Befehlzeilenwerkzeug

# ==============================================================================
# KONTAKT UND SUPPORT
# ==============================================================================

# Repository: https://github.com/puchadave/omni-core-middleware
# Issues: https://github.com/puchadave/omni-core-middleware/issues
# Dokumentation: docs/ Verzeichnis im Repository

# ==============================================================================
# LIZENZ
# ==============================================================================

# Das Projekt ist unter der MIT Lizenz verfügbar.
# Weitere Details: LICENSE Datei im Repository root.

# ==============================================================================
# ENDE DER README
# ==============================================================================