# Omni-Core ERP & BI Enterprise Edition

## Sofortstart in unter 2 Minuten

Dieses Paket enthält alles für die sofortige Bereitstellung der Omni-Core E-Commerce Middleware als Enterprise ERP & BI Lösung.

### Anforderungen
- Docker & Docker Compose (v2.20+)
- Mindestens 512MB RAM
- Ports 5432 (PostgreSQL) und 8000 (Applikation) frei

### Installation & Start (One-Click)

#### Unter Linux/macOS/Proxmox VE (start.sh):
```bash
chmod +x start.sh
./start.sh
```

### Oder per Windows (start.bat):
Doppelklick auf `start.bat`

### Nach dem Start
Das System ist sofort einsatzbereit:

- **Webinterface (WaWi):** http://SERVER-IP:8000/admin/products
- **API Health-Check:** http://localhost:8000/api/health
- **PostgreSQL-Datenbank:** postgresql://omnicore:omnicore_pass@localhost:5432/omnicore

### Standard-Zugangsdaten
- **Datenbank-User:** omnicore
- **Datenbank-Passwort:** omnicore_pass
- **Datenbank-Name:** omnicore

### Wichtige Pfade & Volumes
- **Datenbank-Daten:** `pg_data` (persistent)
- **Applikations-Daten:** `app_data` (Produktbilder, Logs, Reports)
- **Konfigurations-Env-Variablen:** in `docker-compose.yml` und `start.sh`/`start.bat` editierbar

### Zugriff via Docker CLI
```bash
# In den Logs sehen
docker logs -f omni_core

 Datenbank neu starten:
docker restart omni_db

  Vollständiges Neustarten:
  ./start.sh (bzw. start.bat)
```

### PCI-DSS & Security Hinweise
- Ändern Sie die Standard-Passwörter in Produktion
- Nutzen Sie ein eigenes Netzwerk statt `bridge`
- Sichern Sie regelmäßig das `pg_data` Volume
- Die Applikation läuft standardmäßig nur auf Port 8000

### Fragen & Support
Besuchen Sie die Dokumentation oder öffnen Sie ein Issue im GitHub-Repository.

---

**Willkommen bei Omni-Core ERP & BI Enterprise Edition!**