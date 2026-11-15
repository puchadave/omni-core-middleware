# Operations

## Provisioning (Proxmox host)
- Dry run (default-safe): `./install-shopware-omni.sh` or `./install-shopware-omni.sh --dry-run`
- Install: `./install-shopware-omni.sh --ctid <id|auto> --hostname omni-shopware --ip <cidr|dhcp> --gateway <ip-if-static> --shop-domain <domain> --omni-core-url http://<omni>:8002`
- Reconfigure/upgrade (idempotent): add `--force` or `--upgrade`
- Uninstall (controlled, configured CT only): `./install-shopware-omni.sh --ctid <id> --uninstall`

## Health & diagnostics
- `scripts/healthcheck.sh --config <generated-config>` (wrapper) or in-CT `/opt/shopware-deploy/current/healthcheck.sh`
- `scripts/diagnostics.sh` for collected non-secret diagnostics
- Monitor HTTP/API, PHP-FPM, DB, Redis, Search, workers, scheduled tasks, OMNI-CORE/webhook connectivity.

## Updates
- Keep Shopware core/vendor unmodified; use plugins/apps/config/integration layer.
- Re-run with `--upgrade` to apply supported reconfiguration. See `scripts/update.sh`.

## Logs
Separated streams: shopware, php/php-fpm, webserver, database, redis, worker/scheduled-task, integration/webhook (request_id, entity, operation, status, latency, retry_count; no secrets/PII).
