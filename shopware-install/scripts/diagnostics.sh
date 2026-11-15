#!/usr/bin/env bash
set -Eeuo pipefail
# Non-secret diagnostics for OMNI-CORE Shopware node
CTID=""
while [[ $# -gt 0 ]]; do case "$1" in --ctid) CTID="$2"; shift 2;; *) shift;; esac; done
run(){ if command -v pct >/dev/null 2>&1 && [[ -n "$CTID" ]]; then pct exec "$CTID" -- bash -lc "$*"; else bash -lc "$*"; fi; }
echo "=== SHOPWARE NODE DIAGNOSTICS (no secrets) ==="
date -u +"%Y-%m-%dT%H:%M:%SZ" || true
run 'hostname; uname -a' 2>&1 | sed 's/^/HOST: /' || true
run 'systemctl is-active nginx php*-fpm mariadb mysql redis-server shopware-worker 2>/dev/null || true' 2>&1 | head -50 || true
run 'df -h / /var/www 2>/dev/null || df -h' 2>&1 | head -20 || true
run 'ss -ltnp 2>/dev/null | head -30 || true' 2>&1 || true
run 'curl -ksS -o /dev/null -w "http=%{http_code}\n" http://127.0.0.1/ 2>/dev/null || true' 2>&1 || true
run 'test -f /var/log/shopware-deploy/healthcheck.json && cat /var/log/shopware-deploy/healthcheck.json || echo "no health report"' 2>&1 | head -120 || true
echo "=== END ==="
