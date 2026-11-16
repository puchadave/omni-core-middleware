#!/usr/bin/env bash
#
# tune_and_verify_pair.sh
# Align OmniRoute <-> OMNI-CORE settings and run strict pre-template verification.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib_nexus_proxmox.sh
source "${SCRIPT_DIR}/lib_nexus_proxmox.sh"

OMNIROUTE_NODE="LOCAL"
OMNIROUTE_CTID="201"
OMNIROUTE_IP=""
OMNIROUTE_API_KEY=""
OMNICORE_NODE="LOCAL"
OMNICORE_CTID="202"
OMNICORE_IP=""
INTERNAL_API_KEY=""
JSON_OUT=""

usage() {
    cat <<'EOF'
Usage: tune_and_verify_pair.sh [options]
  --omniroute-node LOCAL|SSH_HOST  OmniRoute Proxmox node
  --omniroute-ctid ID              OmniRoute CTID
  --omniroute-ip IP                OmniRoute container IP
  --omniroute-api-key KEY          OmniRoute API key
  --omnicore-node LOCAL|SSH_HOST   OMNI-CORE Proxmox node
  --omnicore-ctid ID               OMNI-CORE CTID
  --omnicore-ip IP                 OMNI-CORE container IP
  --internal-api-key KEY           OMNI-CORE internal API key
  --json-out PATH                  Write machine-readable JSON result to PATH
  -h, --help                       Show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --omniroute-node) OMNIROUTE_NODE="${2:?}"; shift 2 ;;
        --omniroute-ctid) OMNIROUTE_CTID="${2:?}"; shift 2 ;;
        --omniroute-ip) OMNIROUTE_IP="${2:?}"; shift 2 ;;
        --omniroute-api-key) OMNIROUTE_API_KEY="${2:?}"; shift 2 ;;
        --omnicore-node) OMNICORE_NODE="${2:?}"; shift 2 ;;
        --omnicore-ctid) OMNICORE_CTID="${2:?}"; shift 2 ;;
        --omnicore-ip) OMNICORE_IP="${2:?}"; shift 2 ;;
        --internal-api-key) INTERNAL_API_KEY="${2:?}"; shift 2 ;;
        --json-out) JSON_OUT="${2:?}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

require_cmd jq
[ -n "${OMNIROUTE_IP}" ] || die "--omniroute-ip is required"
[ -n "${OMNICORE_IP}" ] || die "--omnicore-ip is required"
[ -n "${OMNIROUTE_API_KEY}" ] || die "--omniroute-api-key is required"
[ -n "${INTERNAL_API_KEY}" ] || die "--internal-api-key is required"

ct_exec_on() {
    local node="$1" ctid="$2" snippet="$3"
    if [ "${node}" = "LOCAL" ]; then
        pct exec "${ctid}" -- bash -lc "${snippet}"
    else
        ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@${node}" \
            "pct exec ${ctid} -- bash -lc $(printf '%q' "${snippet}")"
    fi
}

log_info "Syncing OmniRoute tuning and combos on ${OMNIROUTE_NODE}:${OMNIROUTE_CTID}"
ct_exec_on "${OMNIROUTE_NODE}" "${OMNIROUTE_CTID}" "jq '.timeouts.requestMs=1200 | .timeouts.failoverMs=1400 | .retries[\"429\"]=true | .retries[\"502\"]=true | .retries[\"503\"]=true | .cache.enabled=true | .cache.ttlSeconds=300' /root/.omniroute/config.json > /root/.omniroute/config.json.tmp && mv /root/.omniroute/config.json.tmp /root/.omniroute/config.json && rc-service omniroute restart"
sleep 5

log_info "Syncing OMNI-CORE settings to OmniRoute on ${OMNICORE_NODE}:${OMNICORE_CTID}"
ct_exec_on "${OMNICORE_NODE}" "${OMNICORE_CTID}" "cd /opt/omni-core && DATABASE_PATH=/opt/omni-core/data/omnicore.db DATABASE_URL='' /opt/omni-core/venv/bin/python -c \"
import asyncio
from database import set_setting
async def main():
    await set_setting('omniroute_url', '${OMNIROUTE_IP}'.join(['http://',':20128/v1']))
    await set_setting('omniroute_api_key', '${OMNIROUTE_API_KEY}')
    await set_setting('omniroute_agent_model', 'omni-admin-agent')
    await set_setting('default_llm_model', 'auto/smart')
    await set_setting('ai_fallback_chain', '[\\\"auto/smart\\\",\\\"auto\\\",\\\"auto/cheap\\\"]')
    await set_setting('ai_cache_enabled', '1')
    await set_setting('ai_strict_privacy_mode', '1')
asyncio.run(main())
\""
ct_exec_on "${OMNICORE_NODE}" "${OMNICORE_CTID}" "rc-service omni-core restart"
sleep 8

FAILED=0

log_info "Verify OmniRoute /v1/models"
OMNIROUTE_MODELS_HTTP="$(curl -s -o /tmp/nexus_omniroute_models.json -w '%{http_code}' -H "Authorization: Bearer ${OMNIROUTE_API_KEY}" "http://${OMNIROUTE_IP}:20128/v1/models" || true)"
if [ "${OMNIROUTE_MODELS_HTTP}" != "200" ]; then
    log_err "OmniRoute /v1/models failed (HTTP ${OMNIROUTE_MODELS_HTTP})"
    FAILED=1
else
    log_ok "OmniRoute /v1/models HTTP 200"
fi

COMBO_COUNT="$(ct_exec_on "${OMNIROUTE_NODE}" "${OMNIROUTE_CTID}" "jq '[.combos[].name] | length' /root/.omniroute/config.json" | tr -d '\r')"
if [ "${COMBO_COUNT}" != "6" ]; then
    log_err "Expected 6 OmniRoute combos, found ${COMBO_COUNT}"
    FAILED=1
else
    log_ok "OmniRoute combos active: 6"
fi

log_info "Verify OMNI-CORE health"
HEALTH_HTTP="$(curl -s -o /dev/null -w '%{http_code}' "http://${OMNICORE_IP}:8002/health" || true)"
if [ "${HEALTH_HTTP}" != "200" ]; then
    log_err "OMNI-CORE /health failed (HTTP ${HEALTH_HTTP})"
    FAILED=1
else
    log_ok "OMNI-CORE /health HTTP 200"
fi

log_info "Verify OMNI-CORE -> OmniRoute test-connection"
TEST_CONN_BODY="$(curl -s -H "X-Internal-API-Key: ${INTERNAL_API_KEY}" "http://${OMNICORE_IP}:8002/api/ai-control/test-connection" || true)"
TEST_CONN_HTTP="$(curl -s -o /dev/null -w '%{http_code}' -H "X-Internal-API-Key: ${INTERNAL_API_KEY}" "http://${OMNICORE_IP}:8002/api/ai-control/test-connection" || true)"
if [ "${TEST_CONN_HTTP}" != "200" ]; then
    log_err "OMNI-CORE test-connection failed (HTTP ${TEST_CONN_HTTP})"
    FAILED=1
else
    log_ok "OMNI-CORE test-connection HTTP 200"
fi

log_info "Verify PII privacy guard routes to omni-local-gdpr"
PII_PAYLOAD='{"channel":"whatsapp","from":"+491701234567","text":"Hallo, wo ist meine Bestellung 100234?","order_id":"100234"}'
PII_BODY="$(curl -s -X POST -H "Content-Type: application/json" -H "X-Internal-API-Key: ${INTERNAL_API_KEY}" -d "${PII_PAYLOAD}" "http://${OMNICORE_IP}:8002/api/nexus/omnichannel/inbound" || true)"
PII_LOCAL="$(printf '%s' "${PII_BODY}" | jq -r '.pii_routed_local // false' 2>/dev/null || echo false)"
PII_COMBO="$(printf '%s' "${PII_BODY}" | jq -r '.combo_used // ""' 2>/dev/null || echo "")"
if [ "${PII_LOCAL}" != "true" ] || [ "${PII_COMBO}" != "omni-local-gdpr" ]; then
    log_err "PII privacy guard failed (local=${PII_LOCAL}, combo=${PII_COMBO})"
    FAILED=1
else
    log_ok "PII privacy guard enforced: omni-local-gdpr"
fi

if [ "${FAILED}" -ne 0 ]; then
    JSON="$(jq -n \
        --arg omniroute_ip "${OMNIROUTE_IP}" \
        --arg omnicore_ip "${OMNICORE_IP}" \
        --arg omniroute_http "${OMNIROUTE_MODELS_HTTP}" \
        --arg health_http "${HEALTH_HTTP}" \
        --arg test_http "${TEST_CONN_HTTP}" \
        --arg pii_local "${PII_LOCAL}" \
        --arg pii_combo "${PII_COMBO}" \
        '{ok:false,omniroute_ip:$omniroute_ip,omnicore_ip:$omnicore_ip,omniroute_http:$omniroute_http,health_http:$health_http,test_connection_http:$test_http,pii_local:$pii_local,pii_combo:$pii_combo}')"
    write_json_output "${JSON_OUT}" "${JSON}"
    die "pair verification failed; template packaging must not start"
fi

JSON="$(jq -n \
    --arg omniroute_ip "${OMNIROUTE_IP}" \
    --arg omnicore_ip "${OMNICORE_IP}" \
    --arg omniroute_http "${OMNIROUTE_MODELS_HTTP}" \
    --arg health_http "${HEALTH_HTTP}" \
    --arg test_http "${TEST_CONN_HTTP}" \
    --arg pii_local "${PII_LOCAL}" \
    --arg pii_combo "${PII_COMBO}" \
    '{ok:true,omniroute_ip:$omniroute_ip,omnicore_ip:$omnicore_ip,omniroute_http:$omniroute_http,health_http:$health_http,test_connection_http:$test_http,pii_local:$pii_local,pii_combo:$pii_combo}')"
write_json_output "${JSON_OUT}" "${JSON}"
log_ok "Pair verification green; safe to package templates"
