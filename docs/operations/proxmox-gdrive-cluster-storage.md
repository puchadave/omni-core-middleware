# Proxmox VE: Google Drive als gemeinsamer Cluster-Speicher (ganze Ordner / einzelne Datenträger)

## Ziel
Google Drive als fester, clusterweit verfügbarer Bestandteil des Rechenzentrums/Netzwerks in Proxmox VE einbinden. **Einzelne Google-Drive-Ordner** können als eigene Datenträger/Storages gemappt werden (ISOs, Templates, Backups, Snippets, Import, optional Images/Rootdir) – alles dort anlegbar.

## Architektur
- **Pro Ordner/Storage** ein eigenes rclone-FUSE-Mount + systemd-Service (`rclone-<id>.service`) auf **jedem Node**:
  - `gdrive:<remote_pfad>` -> `<mountpoint>` (z.B. `/mnt/pve/gdrive-iso`)
- **Proxmox Storage** je Mount als `dir` mit `shared 1` in `/etc/pve/storage.cfg` (clusterweit via pmxcfs repliziert).
- **Inhalte pro Storage wählbar:** `iso`, `vztmpl`, `backup`, `snippets`, `import`, `images`, `rootdir` (kombinierbar).

## Presets (Standard-Ordnerstruktur im Drive)
Basis-Pfad im Drive via `--base-path proxmox` (empfohlen: dedizierter Ordner).

| Preset | Storage-ID | Drive-Pfad | Mount | Content |
|---|---|---|---|---|
| datacenter/basic | `gdrive-iso` | `<base>/iso` | `/mnt/pve/gdrive-iso` | `iso,import` |
| datacenter/basic | `gdrive-vztmpl` | `<base>/templates` | `/mnt/pve/gdrive-vztmpl` | `vztmpl` |
| datacenter/basic | `gdrive-backup` | `<base>/backup` | `/mnt/pve/gdrive-backup` | `backup` |
| datacenter/basic | `gdrive-snippets` | `<base>/snippets` | `/mnt/pve/gdrive-snippets` | `snippets` |
| datacenter | `gdrive-import` | `<base>/import` | `/mnt/pve/gdrive-import` | `import` |
| datacenter | `gdrive-images` | `<base>/images` | `/mnt/pve/gdrive-images` | `images,rootdir` |

`--preset datacenter` legt alle obigen als separate Datenträger an (alles möglich). `--preset basic` ohne images/rootdir.

## Individuelle Ordner/Storages (beliebig)
Repeatable `--storage`:
```bash
--storage "id=gdrive-iso,path=MeineDriveOrdner/ISOs,mount=/mnt/pve/gdrive-iso,content=iso,import"
--storage "id=gdrive-templates,path=MeineDriveOrdner/Templates,mount=/mnt/pve/gdrive-templates,content=vztmpl"
--storage "id=gdrive-backups,path=Team/Proxmox/Backups,mount=/mnt/pve/gdrive-backups,content=backup"
```

## Rollout (clusterweit, automatisiert)
Script: `scripts/pve-gdrive-cluster/setup_gdrive_cluster.sh`

```bash
export SSH_PASS='<pass>'   # oder SSH-Keys
bash scripts/pve-gdrive-cluster/setup_gdrive_cluster.sh \
  --all-nodes "192.168.178.250,192.168.178.251,192.168.178.252" \
  --rclone-config ./rclone.conf \
  --remote-name gdrive \
  --base-path proxmox \
  --preset datacenter
```
- Installiert rclone/fuse, verteilt Config, erstellt pro Storage Mount+systemd-Service auf allen Nodes, registriert alle Storages einmal clusterweit (`shared 1`).

Einzeln lokal: `--node-only ...` (Mounts/Services), Storage-Einträge: `--register-storage-only ...`

## rclone Autorisierung (einmalig)
```bash
rclone config  # remote: gdrive, type: drive, scope: drive
```
Headless: `rclone authorize "drive"` lokal, Token in `rclone.conf` (Beispiel: `scripts/pve-gdrive-cluster/rclone-gdrive.conf.example`). Config auf Nodes: `/root/.config/rclone/rclone.conf` (chmod 600).

## Prüfung
```bash
pvesm status
mount | grep rclone
systemctl status rclone-gdrive-iso rclone-gdrive-vztmpl rclone-gdrive-backup
grep -n "^dir: gdrive-" /etc/pve/storage.cfg
# Dateien/ISOs/Templates direkt in jeweilige Drive-Ordner legen oder über PVE hochladen
```

## Hinweise
- **images/rootdir** auf Cloud-FUSE: möglich, aber Latenz/Konsistenz beachten; für produktive VM-Disks bevorzugt lokale/CEPH/NFS-Storages. Backups/ISO/VZtmpl/Snippets/Import sind ideal für Drive.
- VFS-Cache: `/var/cache/rclone-gdrive/<storage-id>` (im Service anpassbar). Logs: `/var/log/rclone-<id>.log`.
- `user_allow_other` in `/etc/fuse.conf` erforderlich (Script setzt dies). Services: enabled + restart=always.
