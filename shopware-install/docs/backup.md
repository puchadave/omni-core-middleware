# Backup & Recovery

## Scope
Shopware database, config, files, media, custom plugins/themes, OMNI-CORE integration config.

## Target
Configurable `backup.target_dir` (must be outside container rootfs; host bind/NFS/Proxmox storage recommended). Not the sole productive storage.

## Automation
Installer provisions `/usr/local/sbin/shopware-backup.sh` and `/usr/local/sbin/shopware-restore.sh` plus scheduled cron and integrity checks (see `deploy/shopware-proxmox/setup-backups.sh`). Optional isolated restore test when enabled.

## Operations
- Run backup: `scripts/backup.sh` or in-CT backup script
- Restore: use generated restore script with explicit confirmation; validate via healthcheck after restore.
- Proxmox vzdump can complement app-level backups (`backup.proxmox_vzdump`).
