# Troubleshooting

- **CTID/hostname exists:** installer is idempotent and refuses overwrite; use `--force/--upgrade` or different CTID.
- **Static IP without gateway:** provide `--gateway`.
- **Not on Proxmox host:** run `install-shopware-omni.sh` on the PVE node (pct/pvesh required for LXC creation).
- **OMNI-CORE unreachable:** Shopware install continues; integration marked `PENDING`. Verify `${OMNI_CORE_BASE_URL}/healthz` or configured endpoint and network reachability (port 8002), then re-run integration/health sync.
- **SSL/Let's Encrypt fails:** requires valid DNS/domain and reachability; use `reverse_proxy`/`selfsigned`/`disabled` for internal bootstrap per config.
- **Workers/scheduled tasks not running:** check systemd timers/services (`shopware-worker`, `shopware-scheduled-task`), logs, and queue backend (Redis).
- **API/webhook auth failures:** rotate/check secrets in secure secrets dir (never in logs), verify least-privilege integration user and webhook HMAC/timestamp config.
- **Reports:** deployment JSON under `deployment.report_file`, health under `monitoring.report_json`.
- **Dry run:** use `--dry-run` to validate plan without changes.
