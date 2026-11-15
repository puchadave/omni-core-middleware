# OMNI-CORE Shopware Commerce Node (Proxmox One-Click)

Idempotenter Proxmox-Helper zur Bereitstellung eines dedizierten Shopware-6-LXC als **Commerce Execution Layer** für OMNI-CORE (OMNI-CORE bleibt zentrale Orchestrierung: AI/PIM/Matterhorn/Pricing/CRM/Marketplace/BI/Event-Bus; Kommunikation nur via API/Webhooks).

## Quick start (auf Proxmox-VE-Host, root)
```bash
# Plan/Dry-Run (Standard bei fehlenden kritischen Parametern, keine Änderungen)
./install-shopware-omni.sh --dry-run --omni-core-url http://<omni-host>:8002

# Provisionierung
./install-shopware-omni.sh --ctid 215 --hostname omni-shopware --cores 6 --memory 12288 --swap 2048 --disk 120G \
  --bridge vmbr0 --ip 10.0.0.50/24 --gateway 10.0.0.1 --dns "1.1.1.1 8.8.8.8" \
  --shop-domain shop.example.com --omni-core-url http://10.0.0.10:8002

# Idempotent reconfigure/upgrade
./install-shopware-omni.sh --ctid 215 --upgrade --omni-core-url http://10.0.0.10:8002

# Kontrollierte Deinstallation (nur konfigurierter CT)
./install-shopware-omni.sh --ctid 215 --uninstall
```

## Eigenschaften
- **Discovery → Plan → Dry-Run → Implement → Test → Verify → Report**; `--dry-run` validiert/plant ohne Änderungen.
- **Idempotent:** erkennt bestehende Container/Ressourcen, kein blindes Überschreiben (`--force/--upgrade` für Reconfigure).
- **Shopware standardnah:** aktuelle stabile, kompatible Version; keine Core/Vendor-Patches.
- **Stack:** PHP-FPM/OPcache, produktive DB (lokal, non-public), Redis (lokal), Shopware-kompatible Suche, Nginx/Web, Worker & Scheduled Tasks, Backup/Health/Monitoring-Hooks.
- **Integration OMNI-CORE (:8002):** API/Webhooks only, least-privilege Tech-User, Webhook-Security (auth/timestamp/replay/idempotent), Sync-States & Konflikte nie still überschrieben; Integration `PENDING` wenn OMNI-CORE temporär unerreichbar.
- **Sicherheit:** Secrets zufällig, nie ausgegeben/geloggt; DB/Redis/Search nicht öffentlich; TLS-ready.

## Artefakte
- `install-shopware-omni.sh` – Haupt-Helper (dieses Interface)
- `deploy/shopware-proxmox/*` – Provisionierungs-/Konfigurationsmodule
- `scripts/{healthcheck,backup,update,diagnostics}.sh`
- `docs/{architecture,omni-core-integration,operations,backup,troubleshooting}.md`
- `.env.example`

## Hinweise
- Ausführung der LXC-Erstellung erfordert einen **Proxmox-VE-Host** (`pct/pvesh`). In dieser Umgebung wurde nur Discovery/Implementierung der Skripte & Doku durchgeführt (kein PVE verfügbar) → tatsächliche Provisionierung/Tests auf Ziel-PVE ausführen.
- Shopware/OMNI-CORE APIs & Parameter werden nicht erfunden; Installer nutzt/validiert offizielle Shopware-Anforderungen und die vorhandenen Deploy-Module.
