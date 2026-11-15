# OMNI-CORE Middleware – Proxmox LXC One-Click Installer

Erstellt einen Debian-12-LXC auf Proxmox VE und deployt die OMNI-CORE Middleware via Docker Compose (PostgreSQL + App).

## One-Click (Copy & Paste auf Proxmox-Host als `root`)

Sicherer Dry-Run (keine Änderungen):

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/puchadave/omni-core-middleware/main/middleware-install/install-omni-core-lxc.sh)" -- --dry-run
```

Installation (Beispiel, DHCP, Port 8002):

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/puchadave/omni-core-middleware/main/middleware-install/install-omni-core-lxc.sh)" -- \
  --apply --ctid 220 --hostname omni-core --cores 4 --memory 4096 --disk 32G \
  --bridge vmbr0 --ip dhcp --port 8002
```

Statische IP:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/puchadave/omni-core-middleware/main/middleware-install/install-omni-core-lxc.sh)" -- \
  --apply --ctid 220 --ip 10.0.0.40/24 --gateway 10.0.0.1 --port 8002
```

Lokal aus dem Repo:

```bash
bash middleware-install/install-omni-core-lxc.sh --dry-run
bash middleware-install/install-omni-core-lxc.sh --apply --ctid 220 --port 8002
```

## Optionen (Auszug)
- `--apply` führt Änderungen aus (ohne: Dry-Run)
- `--ctid`, `--hostname`, `--cores`, `--memory`, `--swap`, `--disk`, `--storage`, `--bridge`, `--ip`, `--gateway`, `--dns`
- `--port` Host-Port (default 8002, Container intern 8000)
- `--repo-url`, `--repo-ref` (default `main`), `--install-dir` (default `/opt/omni-core-middleware`)
- `--force` idempotentes Redeploy/Reconfigure bei bestehender CTID
- `--uninstall --apply --ctid <ID>` kontrollierte Entfernung

## Verhalten & Sicherheit
- Idempotent: bestehende Container werden ohne `--force` nicht überschrieben; `.env` mit Secrets bleibt erhalten.
- Secrets (`POSTGRES_PASSWORD`, `INTERNAL_API_KEY`, `SESSION_SECRET`) werden im Container generiert und nie ausgegeben (`/opt/omni-core-middleware/.env`).
- Health-Check: `/healthz`. Bei `PARTIAL` Logs prüfen: `pct exec <CTID> -- docker compose -f /opt/omni-core-middleware/docker-compose.yml logs --tail=200`.

## Nach der Installation
- OMNI-CORE URL: `http://<CT-IP>:<PORT>` (ggf. Firewall/Port-Forwarding)
- Integrationen/Keys (OmniRoute, Matterhorn, Shopware etc.) über Settings/API/`.env` konfigurieren – keine Secrets ins Repo.
- Shopware-Commerce-Node separat via `shopware-install/install-shopware-omni.sh` bereitstellen; OMNI-CORE bleibt Orchestrator.
