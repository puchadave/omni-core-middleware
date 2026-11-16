#!/usr/bin/env bash
# nexus-cli.sh - lightweight NEXUS/Proxmox Manager API client for CLI agents
set -euo pipefail

BASE_URL="${OMNICORE_BASE_URL:-${NEXUS_BASE_URL:-http://127.0.0.1:8002}}"
API_KEY="${OMNICORE_API_KEY:-${NEXUS_API_KEY:-${INTERNAL_API_KEY:-}}}"
PVE_TOKEN="${PVE_AGENT_TOKEN:-}"
TIMEOUT="${NEXUS_TIMEOUT:-30}"

usage() {
  cat <<'EOF'
Usage: nexus-cli.sh <command> [args]
Env: OMNICORE_BASE_URL (default http://127.0.0.1:8002), OMNICORE_API_KEY|INTERNAL_API_KEY

Commands:
  nexus-status
  nexus-events [limit]
  nexus-skills
  pve-nodes [limit]
  pve-plan "<requirements>" [lxc|vm] [hostname]
  pve-network-plan "<requirements>" [node]
  pve-firewall-plan "<requirements>" [cluster|node|vm] [node]
  pve-action <node> <type> '<params-json>'
  pve-chat "<message>" [node]
  orchestrate <mode> "<task>" [context]   (modes: single|pipeline|parallel|debate|code|ops)
  raw <METHOD> <PATH> [JSON_BODY]
EOF
}

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing dependency: $1" >&2; exit 1; }; }
need curl
if command -v jq >/dev/null 2>&1; then HAVE_JQ=1; else HAVE_JQ=0; fi

auth_args=()
if [ -n "$API_KEY" ]; then auth_args+=( -H "X-Internal-API-Key: ${API_KEY}" ); fi
if [ -n "$PVE_TOKEN" ]; then auth_args+=( -H "X-PVE-Agent-Token: ${PVE_TOKEN}" ); fi

req() {
  local method="$1" path="$2" body="${3:-}"
  local url="${BASE_URL%/}${path}"
  local args=( -fsS --max-time "$TIMEOUT" -X "$method" "$url" -H 'Content-Type: application/json' "${auth_args[@]}" )
  if [ -n "$body" ]; then args+=( --data "$body" ); fi
  curl "${args[@]}"
}

pp() { if [ "$HAVE_JQ" = 1 ]; then jq .; else cat; fi; }

cmd="${1:-help}"; shift || true
case "$cmd" in
  nexus-status) req GET /api/nexus/status | pp ;;
  nexus-events) req GET "/api/nexus/events?limit=${1:-50}" | pp ;;
  nexus-skills) req GET /api/nexus/skills | pp ;;
  pve-nodes) req GET "/api/nexus/pve/nodes?limit=${1:-100}" | pp ;;
  pve-plan)
    req POST /api/nexus/pve/plan "$(printf '{"requirements":%s,"target":%s,"hostname":%s}' "$(printf '%s' "${1:-}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" "$(printf '%s' "${2:-lxc}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))')" "$(printf '%s' "${3:-}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))')")" | pp ;;
  pve-network-plan)
    req POST /api/nexus/pve/network/plan "$(printf '{"requirements":%s,"node":%s}' "$(printf '%s' "${1:-}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" "$(printf '%s' "${2:-*}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))')")" | pp ;;
  pve-firewall-plan)
    req POST /api/nexus/pve/firewall/plan "$(printf '{"requirements":%s,"scope":%s,"node":%s}' "$(printf '%s' "${1:-}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" "$(printf '%s' "${2:-cluster}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))')" "$(printf '%s' "${3:-*}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))')")" | pp ;;
  pve-action)
    node="${1:-*}"; type="${2:-report_now}"; params="${3:-{}}"
    req POST /api/nexus/pve/actions "$(python3 - "$node" "$type" "$params" <<'PY'
import json,sys
node,typ,params=sys.argv[1:4]
try: p=json.loads(params)
except Exception: p={"raw":params}
print(json.dumps({"node":node,"type":typ,"params":p},separators=(',',':')))
PY
)" | pp ;;
  pve-chat)
    req POST /api/nexus/pve/chat "$(printf '{"message":%s,"node":%s}' "$(printf '%s' "${1:-}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" "$(printf '%s' "${2:-*}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))')")" | pp ;;
  orchestrate)
    mode="${1:-pipeline}"; task="${2:-}"; ctx="${3:-}"
    req POST /api/nexus/orchestration/run "$(python3 - "$mode" "$task" "$ctx" <<'PY'
import json,sys
mode,task,ctx=sys.argv[1:4]
print(json.dumps({"mode":mode,"task":task,"context":ctx},separators=(',',':')))
PY
)" | pp ;;
  raw)
    req "${1:-GET}" "${2:-/api/nexus/status}" "${3:-}" | pp ;;
  help|--help|-h) usage ;;
  *) echo "unknown command: $cmd" >&2; usage; exit 2 ;;
esac
