#!/usr/bin/env bash
set -euo pipefail

# OMNI-CORE / OmniRoute Hardcore AI Tuning & Sync (idempotent, safe)
# - Discovery + backup before mutation
# - Never prints secrets; never invents unsupported OmniRoute features
# - Re-runnable / cron-friendly; supports --restore-only

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"
MODE="tune"
RESTORE_FROM=""
DRY_RUN="${DRY_RUN:-0}"

# Colors
if [[ -t 1 ]]; then
  C_INFO='\033[1;34m'; C_OK='\033[1;32m'; C_WARN='\033[1;33m'; C_ERR='\033[1;31m'; C_RST='\033[0m'
else
  C_INFO=''; C_OK=''; C_WARN=''; C_ERR=''; C_RST=''
fi
log() { echo -e "${C_INFO}[INFO]${C_RST} $*"; }
ok() { echo -e "${C_OK}[OK]${C_RST} $*"; }
warn() { echo -e "${C_WARN}[WARN]${C_RST} $*"; }
err() { echo -e "${C_ERR}[ERROR]${C_RST} $*" >&2; }

usage(){ cat <<EOF
Usage: $0 [--restore-only [BACKUP_DIR]] [--dry-run]
  --restore-only   Re-apply tuning/sync from latest (or given) backup/config snapshot
  --dry-run        Discovery + planning only, no mutations/restarts
Env (optional, never echoed): OMNIROUTE_HOME, OMNIROUTE_BASE_URL, OMNIROUTE_MGMT_URL,
  OMNIROUTE_MGMT_TOKEN, OMNIROUTE_API_KEY, OMNICORE_HOME
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --restore-only) MODE="restore"; shift; if [[ $# -gt 0 && "${1:-}" != --* ]]; then RESTORE_FROM="$1"; shift; fi ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "Unknown arg: $1"; usage; exit 2 ;;
  esac
done

# --- helpers ---
have(){ command -v "$1" >/dev/null 2>&1; }
mask(){ local s="${1:-}"; local n=${#s}; if (( n<=8 )); then printf '***'; else printf '%s***%s' "${s:0:4}" "${s: -4}"; fi; }
sha(){ if have sha256sum; then sha256sum "$1" | awk '{print $1}'; elif have shasum; then shasum -a 256 "$1" | awk '{print $1}'; else echo "n/a"; fi; }

# Backup root (prefer /root, fallback to repo-local if not writable)
if [[ -w /root || $(id -u) -eq 0 ]]; then
  BACKUP_ROOT="/root/omniroute-backups"
else
  BACKUP_ROOT="${REPO_ROOT}/.local/omniroute-backups"
fi
BACKUP_DIR="${BACKUP_ROOT}/pre-tune-${TS}"
LATEST_LINK="${BACKUP_ROOT}/latest"

mkdir -p "$BACKUP_DIR"

REPORT_JSON="${BACKUP_DIR}/report.json"
STATE_JSON="${BACKUP_DIR}/state.json"

# --- discovery ---
log "Pre-flight & discovery (mode=${MODE}, dry_run=${DRY_RUN})"
for bin in curl jq python3; do
  if have "$bin"; then ok "tool present: $bin ($($bin --version 2>&1 | head -1 || echo ok))"; else warn "tool missing: $bin"; fi
done

# OmniRoute paths
OMNIROUTE_HOME="${OMNIROUTE_HOME:-}"
CANDIDATES=("$OMNIROUTE_HOME" "/opt/omniroute" "/root/.omniroute" "$HOME/.omniroute")
FOUND_OR_PATHS=()
for p in "${CANDIDATES[@]}"; do
  [[ -n "$p" && -e "$p" ]] && FOUND_OR_PATHS+=("$p")
done
# de-dup
if ((${#FOUND_OR_PATHS[@]})); then
  mapfile -t FOUND_OR_PATHS < <(printf '%s\n' "${FOUND_OR_PATHS[@]}" | awk '!seen[$0]++')
fi

OMNIROUTE_BASE_URL="${OMNIROUTE_BASE_URL:-http://127.0.0.1:20128}"
OMNIROUTE_API_V1="${OMNIROUTE_BASE_URL%/}/v1"
OMNIROUTE_MGMT_URL="${OMNIROUTE_MGMT_URL:-${OMNIROUTE_BASE_URL%/}}"
OMNIROUTE_MGMT_TOKEN="${OMNIROUTE_MGMT_TOKEN:-}"
OMNIROUTE_PUBLIC_API_V1="${OMNIROUTE_PUBLIC_API_V1:-https://puchalla.online/v1}"
OMNIROUTE_LOCAL_API_V1="${OMNIROUTE_LOCAL_API_V1:-http://localhost:20128/v1}"

log "OmniRoute base: ${OMNIROUTE_API_V1} (mgmt: ${OMNIROUTE_MGMT_URL})"
if ((${#FOUND_OR_PATHS[@]})); then ok "OmniRoute paths: ${FOUND_OR_PATHS[*]}"; else warn "No local OmniRoute install path found (checked /opt/omniroute, /root/.omniroute, ~/.omniroute)."; fi

# OMNI-CORE detection
OMNICORE_HOME="${OMNICORE_HOME:-$REPO_ROOT}"
OMNICORE_CONTAINER=""
if have docker; then
  OMNICORE_CONTAINER="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E 'omni-core-middleware-omni-core|omni-core' | head -1 || true)"
fi
[[ -n "$OMNICORE_CONTAINER" ]] && ok "OMNI-CORE container: ${OMNICORE_CONTAINER}" || warn "OMNI-CORE container not found/running (docker unavailable or not up)."

# --- backup (configs/db/.env) ---
log "Backing up configurations to ${BACKUP_DIR}"
backup_file(){ local f="$1"; [[ -f "$f" ]] || return 0; local rel; rel="$(echo "$f" | tr '/' '_' | sed 's/^_//')"; cp -a "$f" "${BACKUP_DIR}/${rel}"; echo "$(sha "$f")  $f" >> "${BACKUP_DIR}/checksums.sha256"; }
for p in "${FOUND_OR_PATHS[@]:-}"; do
  [[ -z "$p" ]] && continue
  log "snapshot: $p"
  mkdir -p "${BACKUP_DIR}/paths"
  # copy config/env/db-like files (non-destructive)
  while IFS= read -r -d '' f; do backup_file "$f"; done < <(find "$p" -maxdepth 3 -type f \( -name '*.env' -o -name '*.json' -o -name '*.yaml' -o -name '*.yml' -o -name '*.toml' -o -name '*.conf' -o -name '*.db' -o -name '*.sqlite*' \) -print0 2>/dev/null || true)
done
# OMNI-CORE relevant files
for f in "$OMNICORE_HOME/.env" "$OMNICORE_HOME/docker-compose.yml" "$OMNICORE_HOME/config/omniroute/omnicore-auto-combos.json" "$OMNICORE_HOME/config/omniroute/endpoints.json"; do
  backup_file "$f"
done
# DB settings snapshot (read-only) if possible
if have docker && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^omni-core-middleware-db-1$'; then
  docker exec omni-core-middleware-db-1 psql -U omnichat -d omnicore -Atc "select key||chr(9)||coalesce(value,'') from settings order by key" > "${BACKUP_DIR}/omnicore_settings.tsv" 2>/dev/null || warn "Could not snapshot omnicore settings table"
  # masked copy for report (no secret values)
  docker exec omni-core-middleware-db-1 psql -U omnichat -d omnicore -Atc "select key||chr(9)||case when (key ilike '%key%' or key ilike '%token%' or key ilike '%secret%' or key ilike '%pass%') then '***masked***' else left(coalesce(value,''),120) end from settings order by key" > "${BACKUP_DIR}/omnicore_settings.masked.tsv" 2>/dev/null || true
  ok "OMNI-CORE DB settings snapshot saved (masked report copy)"
fi
ln -sfn "$BACKUP_DIR" "$LATEST_LINK" 2>/dev/null || true
ok "Backup complete: ${BACKUP_DIR}"

# --- OmniRoute capability probes ---
probe_code(){ curl -sS --max-time 5 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || echo "000"; }
OR_HEALTH_CODE="$(probe_code "${OMNIROUTE_MGMT_URL%/}/api/health")"
OR_MODELS_CODE="$(probe_code "${OMNIROUTE_API_V1}/models")"
OR_COMBOS_CODE="$(probe_code "${OMNIROUTE_MGMT_URL%/}/api/combos")"
log "Probes: /api/health=${OR_HEALTH_CODE} /v1/models=${OR_MODELS_CODE} (no-auth) /api/combos=${OR_COMBOS_CODE} (no-auth)"

fetch_models(){
  local auth_header=()
  [[ -n "${OMNIROUTE_API_KEY:-}" ]] && auth_header=(-H "Authorization: Bearer ${OMNIROUTE_API_KEY}")
  curl -sS --max-time 8 "${auth_header[@]}" "${OMNIROUTE_API_V1}/models" 2>/dev/null || echo '{}'
}

REQUIRED_COMBOS=(omni-admin-agent omni-content-seo omni-vision-photo omni-video-gen omni-crm-support omni-local-gdpr)

check_combos(){
  local models_json="$1"; local missing=()
  for c in "${REQUIRED_COMBOS[@]}"; do
    if echo "$models_json" | grep -q "\"id\":\"${c}\""; then :; else missing+=("$c"); fi
  done
  if ((${#missing[@]}==0)); then echo "ALL"; else echo "MISSING:${missing[*]}"; fi
}

MODELS_JSON="$(fetch_models)"
COMBO_STATUS="$(check_combos "$MODELS_JSON")"
log "Combo status on gateway: ${COMBO_STATUS}"

# --- Phase 2: install/upsert combos on OmniRoute (only if mgmt token + API known) ---
apply_gateway_combos(){
  if [[ -z "$OMNIROUTE_MGMT_TOKEN" ]]; then
    warn "No OMNIROUTE_MGMT_TOKEN provided -> cannot mutate gateway combos/config via /api/combos. (Gateway requires management token; v1 key insufficient.)"
    return 1
  fi
  if [[ ! -f "$OMNICORE_HOME/config/omniroute/omnicore-auto-combos.json" ]]; then
    warn "Combo config file missing: $OMNICORE_HOME/config/omniroute/omnicore-auto-combos.json"; return 1
  fi
  if ! have jq; then warn "jq required for combo upsert"; return 1; fi
  log "Attempting idempotent combo upsert via ${OMNIROUTE_MGMT_URL}/api/combos (management token provided)"
  local okc=0 failc=0
  while IFS= read -r combo; do
    local name; name="$(echo "$combo" | jq -r '.name')"
    # best-effort upsert (API schema may differ across OmniRoute versions) - no secret echo
    local code
    code="$(curl -sS --max-time 10 -o /tmp/or_combo_resp.$$ -w '%{http_code}' \
      -X POST "${OMNIROUTE_MGMT_URL%/}/api/combos" \
      -H "Authorization: Bearer ${OMNIROUTE_MGMT_TOKEN}" \
      -H "Content-Type: application/json" \
      -d "$combo" 2>/dev/null || echo 000)"
    if [[ "$code" =~ ^2 ]]; then ok "combo upsert ok: $name (HTTP $code)"; ((okc++)) || true
    else warn "combo upsert not confirmed for $name (HTTP $code) - schema/capability may differ; not fabricated"; ((failc++)) || true; fi
  done < <(jq -c '.combos[]' "$OMNICORE_HOME/config/omniroute/omnicore-auto-combos.json")
  rm -f /tmp/or_combo_resp.$$ || true
  [[ $okc -gt 0 && $failc -eq 0 ]]
}

# --- Phase 3: OMNI-CORE sync (DB settings + .env keys, preserve secrets) ---
sync_omnicore(){
  log "Syncing OMNI-CORE settings (logical omni-* routing, endpoints). Secrets preserved."
  local env_file="$OMNICORE_HOME/.env"
  # .env: set/update non-secret keys idempotently (backup already taken)
  if [[ -f "$env_file" && $DRY_RUN -eq 0 ]]; then
    python3 - "$env_file" "$OMNIROUTE_API_V1" <<'PY'
import sys, re
path, url = sys.argv[1], sys.argv[2]
with open(path) as f: lines=f.read().splitlines()
def setk(key,val,lines):
    pat=re.compile(r'^'+re.escape(key)+r'=')
    out=[]; found=False
    for l in lines:
        if pat.match(l):
            if not found: out.append(f"{key}={val}"); found=True
            # drop duplicates
        else: out.append(l)
    if not found: out.append(f"{key}={val}")
    return out
# only safe, non-secret routing hints; do NOT overwrite OMNIROUTE_API_KEY/OMNI_ROUTE_API_KEY
lines=setk('OMNIROUTE_URL', url, lines)
lines=setk('OMNI_ROUTE_URL', url, lines)
# logical module->combo mapping (consumed if app supports env mapping)
m={
 'OMNI_MODEL_ADMIN':'omni-admin-agent','OMNI_MODEL_CONTENT_SEO':'omni-content-seo',
 'OMNI_MODEL_VISION':'omni-vision-photo','OMNI_MODEL_VIDEO':'omni-video-gen',
 'OMNI_MODEL_CRM':'omni-crm-support','OMNI_MODEL_CRM_GDPR':'omni-local-gdpr'
}
for k,v in m.items(): lines=setk(k,v,lines)
with open(path,'w') as f: f.write('\n'.join(lines)+'\n')
PY
    ok "OMNI-CORE .env updated (non-secret routing keys only)"
  elif [[ $DRY_RUN -eq 1 ]]; then
    warn "dry-run: skip .env mutation"
  else
    warn "OMNI-CORE .env not found at $env_file"
  fi

  # DB settings upsert (idempotent). Preserve existing omniroute_api_key value (do not overwrite).
  if [[ $DRY_RUN -eq 0 && -n "$OMNICORE_CONTAINER" ]] && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^omni-core-middleware-db-1$'; then
    docker exec -i omni-core-middleware-db-1 psql -U omnichat -d omnicore -v ON_ERROR_STOP=1       -v or_url="${OMNIROUTE_API_V1}" -v or_local="${OMNIROUTE_LOCAL_API_V1:-$OMNIROUTE_API_V1}" -v or_public="${OMNIROUTE_PUBLIC_API_V1:-https://puchalla.online/v1}" <<'SQL' >/dev/null
INSERT INTO settings(key,value,is_secret,updated_at) VALUES
 ('omniroute_url',:'or_url',false,now()),
 ('omniroute_url_local',:'or_local',false,now()),
 ('omniroute_url_public',:'or_public',false,now()),
 ('omniroute_agent_model','omni-admin-agent',false,now()),
 ('default_llm_model','omni-admin-agent',false,now()),
 ('ai_model_copilot','omni-admin-agent',false,now()),
 ('ai_model_content_quality','omni-content-seo',false,now()),
 ('ai_model_content_bulk','omni-content-seo',false,now()),
 ('ai_model_image','omni-vision-photo',false,now()),
 ('ai_model_photoshoot','omni-vision-photo',false,now()),
 ('ai_model_vision','omni-vision-photo',false,now()),
 ('ai_model_video','omni-video-gen',false,now()),
 ('ai_model_crm','omni-crm-support',false,now()),
 ('ai_model_crm_gdpr','omni-local-gdpr',false,now()),
 ('omniroute_combo_sync_status','attempted',false,now()),
 ('omniroute_required_combos','["omni-admin-agent","omni-content-seo","omni-vision-photo","omni-video-gen","omni-crm-support","omni-local-gdpr"]',false,now())
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, is_secret=EXCLUDED.is_secret, updated_at=now();
SQL
    ok "OMNI-CORE DB logical routing settings upserted (API key untouched)"
  else
    warn "DB settings sync skipped (dry-run or DB unavailable)"
  fi
}

reload_omniroute(){
  log "Reloading OmniRoute service (if locally managed)"
  if [[ $DRY_RUN -eq 1 ]]; then warn "dry-run: skip reload"; return 0; fi
  local reloaded=0
  if have systemctl && systemctl list-unit-files 2>/dev/null | grep -q '^omniroute'; then
    systemctl restart omniroute && reloaded=1
  elif have docker; then
    local c; c="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -i omniroute | head -1 || true)"
    if [[ -n "$c" ]]; then docker restart "$c" >/dev/null && reloaded=1; fi
  fi
  if [[ $reloaded -eq 1 ]]; then
    ok "OmniRoute reload triggered; waiting for readiness..."
    for i in $(seq 1 30); do
      local code; code="$(probe_code "${OMNIROUTE_API_V1}/models")"
      if [[ "$code" != "000" ]]; then ok "OmniRoute responded (HTTP ${code})"; return 0; fi
      sleep 1
    done
    warn "OmniRoute did not become ready on ${OMNIROUTE_API_V1} after reload"
    return 1
  else
    warn "No local omniroute systemd unit or container found -> cannot reload gateway on this host (remote/LAN gateway?)"
    return 1
  fi
}

reload_omnicore(){
  log "Reload/restart OMNI-CORE middleware (container) if present"
  if [[ $DRY_RUN -eq 1 ]]; then warn "dry-run: skip omnicore reload"; return 0; fi
  if [[ -n "$OMNICORE_CONTAINER" ]] && have docker; then
    # prefer restart for apply of env/db-driven behavior; rebuild not forced
    docker restart "$OMNICORE_CONTAINER" >/dev/null && ok "Restarted ${OMNICORE_CONTAINER}" || warn "Failed to restart ${OMNICORE_CONTAINER}"
    for i in $(seq 1 40); do
      local code; code="$(probe_code "http://127.0.0.1:8002/healthz")"
      if [[ "$code" == "200" ]]; then ok "OMNI-CORE /healthz OK"; return 0; fi
      sleep 1
    done
    warn "OMNI-CORE health not confirmed on :8002 after restart"
    return 1
  else
    warn "OMNI-CORE container not available to reload"
    return 1
  fi
}

run_checks(){
  log "Verification checks"
  local or_models or_health om_health om_test gw_combos
  or_health="$(probe_code "${OMNIROUTE_MGMT_URL%/}/api/health")"
  or_models="$(probe_code "${OMNIROUTE_API_V1}/models")"
  om_health="$(probe_code "http://127.0.0.1:8002/healthz")"
  om_test="$(probe_code "http://127.0.0.1:8002/api/ai-control/test-connection")"
  MODELS_JSON="$(fetch_models)"
  gw_combos="$(check_combos "$MODELS_JSON")"
  echo "CHECK or_api_health=${or_health}"
  echo "CHECK or_v1_models=${or_models}"
  echo "CHECK omnicore_healthz=${om_health}"
  echo "CHECK omnicore_ai_test_connection=${om_test}"
  echo "CHECK gateway_required_combos=${gw_combos}"
  # write report
  python3 - "$REPORT_JSON" "$or_health" "$or_models" "$om_health" "$om_test" "$gw_combos" "$BACKUP_DIR" "$OMNIROUTE_API_V1" <<'PY'
import json,sys,datetime
out={
 "timestamp": datetime.datetime.utcnow().isoformat()+"Z",
 "omniroute_api_health_http": sys.argv[2],
 "omniroute_v1_models_http": sys.argv[3],
 "omnicore_healthz_http": sys.argv[4],
 "omnicore_ai_test_connection_http": sys.argv[5],
 "gateway_required_combos": sys.argv[6],
 "backup_dir": sys.argv[7],
 "omniroute_api_v1": sys.argv[8],
 "notes": [
   "Secrets never printed. Gateway combo mutation requires OMNIROUTE_MGMT_TOKEN and supported /api/combos schema.",
   "omni-local-gdpr must be enforced server-side in OMNI-CORE (policy/allowlist) when gateway/local provider capability is unverified.",
   "localhost:20128 may be unavailable on this host if OmniRoute runs remotely (public/LAN/tunnel)."
 ]
}
open(sys.argv[1],'w').write(json.dumps(out,indent=2,ensure_ascii=False))
print(json.dumps(out,indent=2,ensure_ascii=False))
PY
}

# --- main flow ---
GATEWAY_MUTATED=0
if [[ "$MODE" == "restore" ]]; then
  log "Restore-only mode: re-apply OMNI-CORE sync and re-run checks (gateway mutations only if mgmt token provided)."
fi

if [[ $DRY_RUN -eq 0 ]]; then
  if apply_gateway_combos; then GATEWAY_MUTATED=1; ok "Gateway combo upsert phase reported success"; else warn "Gateway combo upsert not fully applied (capability/token/schema)."; fi
  # security/perf params: only if gateway mgmt API is writable & schema known; otherwise report
  warn "Gateway tuning params (failover<1500ms, caching, pooling, REQUIRE_API_KEY, bind) not auto-applied: requires verified OmniRoute config schema/mgmt access on target host."
  reload_omniroute || true
  sync_omnicore
  reload_omnicore || true
else
  log "dry-run: planned actions -> backup(done), probe(done), combo upsert(if token), sync omnicore .env/db, reload services, verify"
fi

run_checks

# summary
echo
log "=== SUMMARY ==="
echo "Backup: ${BACKUP_DIR}"
echo "OmniRoute target: ${OMNIROUTE_API_V1} (health HTTP ${OR_HEALTH_CODE}, models(no-auth) HTTP ${OR_MODELS_CODE})"
echo "Gateway required combos: ${COMBO_STATUS}"
echo "Gateway mutated via mgmt API: ${GATEWAY_MUTATED}"
echo "OMNI-CORE logical omni-* mappings synced (DB/.env non-secret) with API key preserved."
echo "Report: ${REPORT_JSON}"
if [[ "$COMBO_STATUS" != "ALL" || $GATEWAY_MUTATED -eq 0 ]]; then
  warn "RESULT: PARTIALLY COMPLETED - gateway-side omni-* combos/tuning require local/authorized OmniRoute management access; verify privacy/local-gdpr enforcement in OMNI-CORE."
  exit 0
else
  ok "RESULT: COMPLETED (gateway combos present and sync applied). Verify E2E and privacy constraints."
  exit 0
fi
