#!/usr/bin/env bash
#
# omni-pve-agent.sh - Proxmox VE node monitoring & authorized action agent for OMNI-CORE NEXUS
# Collects telemetry, polls action queue (install/tune/fix/backup/plan-exec), reports results.
#
set -euo pipefail

CONF_FILE="${OMNI_PVE_AGENT_CONF:-/etc/omni-pve-agent.conf}"
if [ -f "$CONF_FILE" ]; then
  # shellcheck disable=SC1090
  . "$CONF_FILE"
fi

MIDDLEWARE_URL="${MIDDLEWARE_URL:-${OMNI_MIDDLEWARE_URL:-}}"
AGENT_TOKEN="${AGENT_TOKEN:-${PVE_AGENT_TOKEN:-${INTERNAL_API_KEY:-}}}"
REPORT_ENDPOINT="${REPORT_ENDPOINT:-/api/nexus/pve/report}"
POLL_ENDPOINT="${POLL_ENDPOINT:-/api/nexus/pve/actions/poll}"
RESULT_ENDPOINT="${RESULT_ENDPOINT:-/api/nexus/pve/actions/result}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-60}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-30}"
NODE_NAME="${NODE_NAME:-$(hostname -s 2>/dev/null || hostname)}"
ENABLE_ACTIONS="${ENABLE_ACTIONS:-1}"
AGENT_VERSION="1.1.0"

log() { echo "[omni-pve-agent $(date -u +%H:%M:%SZ)] $*" >&2; }

collect_once() {
  local ts pvev kernel up load1 load5 load15 mem_total mem_avail mem_used swap_total swap_free
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  pvev="$(pveversion 2>/dev/null | head -n1 || echo "")"
  kernel="$(uname -r 2>/dev/null || echo "")"
  up="$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 0)"
  read -r load1 load5 load15 _ _ < /proc/loadavg 2>/dev/null || { load1=0; load5=0; load15=0; }
  mem_total=$(awk '/MemTotal/{print $2*1024}' /proc/meminfo 2>/dev/null || echo 0)
  mem_avail=$(awk '/MemAvailable/{print $2*1024}' /proc/meminfo 2>/dev/null || echo 0)
  mem_used=$((mem_total>mem_avail ? mem_total-mem_avail : 0))
  swap_total=$(awk '/SwapTotal/{print $2*1024}' /proc/meminfo 2>/dev/null || echo 0)
  swap_free=$(awk '/SwapFree/{print $2*1024}' /proc/meminfo 2>/dev/null || echo 0)

  local cpu_pct=0
  if [ -r /proc/stat ]; then
    read -r _ u n s i iw irq sirq st _ < /proc/stat
    local idle1=$((i+iw)) total1=$((u+n+s+i+iw+irq+sirq+st))
    sleep 0.2
    read -r _ u n s i iw irq sirq st _ < /proc/stat
    local idle2=$((i+iw)) total2=$((u+n+s+i+iw+irq+sirq+st))
    local dt=$((total2-total1)) di=$((idle2-idle1))
    if [ "$dt" -gt 0 ]; then cpu_pct=$(awk "BEGIN{printf \"%.2f\", (1-$di/$dt)*100}"); fi
  fi

  local root_total=0 root_used=0 root_avail=0 root_pct=0
  if df -B1 / >/dev/null 2>&1; then
    read -r _ root_total root_used root_avail root_pct _ < <(df -B1 / | awk 'NR==2{gsub(/%/, "", $5); print $2,$3,$4,$5}')
  fi

  local lxc_running=0 lxc_total=0 vm_running=0 vm_total=0
  if command -v pvesh >/dev/null 2>&1; then
    lxc_total=$(pvesh get /nodes/"$NODE_NAME"/lxc --output-format json 2>/dev/null | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)
    lxc_running=$(pvesh get /nodes/"$NODE_NAME"/lxc --output-format json 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(sum(1 for x in d if str(x.get("status"))=="running"))' 2>/dev/null || echo 0)
    vm_total=$(pvesh get /nodes/"$NODE_NAME"/qemu --output-format json 2>/dev/null | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)
    vm_running=$(pvesh get /nodes/"$NODE_NAME"/qemu --output-format json 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(sum(1 for x in d if str(x.get("status"))=="running"))' 2>/dev/null || echo 0)
  fi

  local storages_json='[]'
  if command -v pvesh >/dev/null 2>&1; then
    storages_json=$(pvesh get /nodes/"$NODE_NAME"/storage --output-format json 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); out=[{"storage":x.get("storage"),"type":x.get("type"),"active":x.get("active"),"total":x.get("total"),"used":x.get("used"),"avail":x.get("avail"),"used_fraction":x.get("used_fraction"),"content":x.get("content")} for x in d]; print(json.dumps(out,separators=(",",":")))' 2>/dev/null || echo '[]')
  fi

  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}' || echo "")"

  python3 - "$NODE_NAME" "$ts" "$pvev" "$kernel" "$up" "$load1" "$load5" "$load15" "$cpu_pct" "$mem_total" "$mem_used" "$mem_avail" "$swap_total" "$swap_free" "$root_total" "$root_used" "$root_avail" "$root_pct" "$lxc_running" "$lxc_total" "$vm_running" "$vm_total" "$storages_json" "$ip" "$AGENT_VERSION" <<'PY'
import json,sys
(node,ts,pvev,kernel,up,l1,l5,l15,cpu,mt,mu,ma,st,sf,rt,ru,ra,rp,lr,lt,vr,vt,sjson,ip,aver)=sys.argv[1:27]
def f(x):
  try: return float(x)
  except: return 0.0
def i(x):
  try: return int(float(x))
  except: return 0
try: stor=json.loads(sjson)
except Exception: stor=[]
print(json.dumps({
  "node":node,"hostname":node,"timestamp":ts,"pve_version":pvev,"kernel":kernel,
  "uptime_seconds":i(up),"loadavg":[f(l1),f(l5),f(l15)],"cpu":{"usage_percent":f(cpu)},
  "memory":{"total_bytes":i(mt),"used_bytes":i(mu),"available_bytes":i(ma)},
  "swap":{"total_bytes":i(st),"free_bytes":i(sf)},
  "rootfs":{"total_bytes":i(rt),"used_bytes":i(ru),"available_bytes":i(ra),"used_percent":f(rp)},
  "guests":{"lxc_running":i(lr),"lxc_total":i(lt),"qemu_running":i(vr),"qemu_total":i(vt)},
  "storages":stor,"ip":ip,"agent_version":aver
}, separators=(',',':')))
PY
}

api_post() {
  local endpoint="$1" payload="$2"
  local url="${MIDDLEWARE_URL%/}${endpoint}"
  curl -fsS --max-time 20 -X POST "$url" \
    -H "Content-Type: application/json" \
    -H "X-PVE-Agent-Token: ${AGENT_TOKEN}" \
    --data "$payload"
}

api_get() {
  local endpoint="$1"
  local url="${MIDDLEWARE_URL%/}${endpoint}"
  curl -fsS --max-time 20 -H "X-PVE-Agent-Token: ${AGENT_TOKEN}" "$url"
}

send_report() { local p; p="$(collect_once)"; api_post "$REPORT_ENDPOINT" "$p" >/dev/null; }

send_result() {
  local action_id="$1" ok="$2" output="$3" err="$4"
  local payload
  payload=$(python3 - "$action_id" "$NODE_NAME" "$ok" "$output" "$err" <<'PY'
import json,sys
aid,node,ok,out,err=sys.argv[1:6]
print(json.dumps({"action_id":aid,"node":node,"ok":ok.lower()=="true","output":out[:8000],"error":err[:4000]},separators=(',',':')))
PY
)
  api_post "$RESULT_ENDPOINT" "$payload" >/dev/null || true
}

run_tune() {
  # Safe, idempotent PVE host tuning
  mkdir -p /etc/sysctl.d
  cat >/etc/sysctl.d/99-omni-nexus-tuning.conf <<'EOF'
vm.swappiness=10
vm.dirty_ratio=15
vm.dirty_background_ratio=5
vm.vfs_cache_pressure=50
fs.inotify.max_user_watches=1048576
fs.inotify.max_user_instances=1024
net.core.somaxconn=4096
net.core.netdev_max_backlog=16384
net.ipv4.tcp_fin_timeout=15
net.ipv4.tcp_keepalive_time=600
net.ipv4.tcp_max_syn_backlog=8192
EOF
  sysctl --system >/dev/null 2>&1 || sysctl -p /etc/sysctl.d/99-omni-nexus-tuning.conf >/dev/null 2>&1 || true
  systemctl enable --now ksmtuned >/dev/null 2>&1 || true
  systemctl enable --now fstrim.timer >/dev/null 2>&1 || true
  echo "tune_applied"
}

run_fix_common() {
  # Common safe fixes: restart reporting services, ensure time sync, clean apt cache, restart pveproxy/pvedaemon if failed
  systemctl restart systemd-timesyncd >/dev/null 2>&1 || true
  apt-get clean >/dev/null 2>&1 || true
  for svc in pvedaemon pveproxy pvestatd; do
    if systemctl is-enabled "$svc" >/dev/null 2>&1 && ! systemctl is-active "$svc" >/dev/null 2>&1; then
      systemctl restart "$svc" >/dev/null 2>&1 || true
      echo "restarted:$svc"
    fi
  done
  echo "fix_common_done"
}

execute_action() {
  local action_json="$1"
  local id type ok=true out="" err=""
  id=$(echo "$action_json" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("id",""))')
  type=$(echo "$action_json" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("type","noop"))')
  log "executing action $id type=$type"
  if [ "${ENABLE_ACTIONS}" != "1" ]; then
    send_result "$id" "false" "" "actions_disabled"; return 0
  fi
  set +e
  case "$type" in
    tune_node|tune) out=$(run_tune 2>&1) ;;
    fix_common|fix) out=$(run_fix_common 2>&1) ;;
    backup_ct)
      ctid=$(echo "$action_json" | python3 -c 'import sys,json; print((json.load(sys.stdin).get("params") or {}).get("ctid",""))')
      mode=$(echo "$action_json" | python3 -c 'import sys,json; print((json.load(sys.stdin).get("params") or {}).get("mode","snapshot"))')
      [ -n "$ctid" ] && out=$(vzdump "$ctid" --mode "$mode" --compress zstd --dumpdir /var/lib/vz/dump 2>&1 | tail -n 60) || { ok=false; err="missing_ctid"; }
      ;;
    pct_exec)
      ctid=$(echo "$action_json" | python3 -c 'import sys,json; print((json.load(sys.stdin).get("params") or {}).get("ctid",""))')
      cmd=$(echo "$action_json" | python3 -c 'import sys,json; print((json.load(sys.stdin).get("params") or {}).get("cmd",""))')
      [ -n "$ctid" ] && [ -n "$cmd" ] && out=$(pct exec "$ctid" -- bash -lc "$cmd" 2>&1 | tail -n 120) || { ok=false; err="missing_ctid_or_cmd"; }
      ;;
    create_lxc)
      # Controlled LXC creation from plan params (local storage, alpine amd64)
      spec=$(echo "$action_json" | python3 -c 'import sys,json; print(json.dumps(json.load(sys.stdin).get("params") or {}))')
      out=$(python3 - "$spec" <<'PY'
import json,subprocess,sys
p=json.loads(sys.argv[1])
ctid=str(p.get('ctid','')).strip()
if not ctid:
  # auto next id
  try: ctid=subprocess.check_output(['pvesh','get','/cluster/nextid'],text=True).strip()
  except Exception: ctid='300'
hostname=p.get('hostname','nexus-lxc')
cores=int(p.get('cores',2)); mem=int(p.get('memory_mb',2048)); disk=int(p.get('disk_gb',8))
bridge=p.get('bridge','vmbr0'); ip=p.get('ip','dhcp')
tmpl=p.get('ostemplate') or 'local:vztmpl/$(pveam available --section system | awk "/alpine-3\..*-amd64/ {print \$2}" | sort -V | tail -n1)'
# find alpine template
try:
  avail=subprocess.check_output("pveam available --section system | awk '/alpine-3\..*-amd64/ {print $2}' | sort -V | tail -n1", shell=True, text=True).strip()
  if avail: tmpl=f"local:vztmpl/{avail}"
except Exception: pass
try:
  subprocess.check_call(['pveam','update'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  subprocess.check_call(['pveam','download','local',tmpl.split('/')[-1]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
except Exception: pass
net=f"name=eth0,bridge={bridge},ip={ip}"
cmd=['pct','create',ctid,tmpl,'--hostname',hostname,'--cores',str(cores),'--memory',str(mem),'--storage','local','--rootfs',f'local:{disk}','--net0',net,'--ostype','alpine','--unprivileged','1','--features','nesting=1,keyctl=1','--onboot','1']
r=subprocess.run(cmd,capture_output=True,text=True)
print(r.stdout+r.stderr)
if r.returncode==0:
  subprocess.run(['pct','start',ctid],capture_output=True,text=True)
  print(f'CTID={ctid}')
sys.exit(r.returncode)
PY
      2>&1) || ok=false ;;
    report_now) out=$(send_report 2>&1 && echo "reported") || ok=false ;;
    apply_firewall)
      fw_spec=$(echo "$action_json" | python3 -c 'import sys,json; print(json.dumps(json.load(sys.stdin).get("params") or {}))')
      out=$(python3 - "$fw_spec" <<'PY'
import json,subprocess,sys
p=json.loads(sys.argv[1])
dry=bool(p.get('dry_run', False))
scope=str(p.get('scope') or 'cluster')
cmds=p.get('commands') or []
rules=p.get('rules') or []
enable=p.get('enable')
lines=[]
if dry:
  lines.append('DRY_RUN=1')
  if enable is not None: lines.append(f'enable_firewall={enable} scope={scope}')
  for c in cmds: lines.append('CMD: '+str(c))
  for r in rules: lines.append('RULE: '+json.dumps(r,ensure_ascii=False))
  print('\n'.join(lines) or 'dry_run_noop')
  sys.exit(0)
# enable firewall toggles (best-effort, cluster-level)
try:
  if enable is True and scope=='cluster':
    subprocess.run(['pvesh','set','/cluster/firewall/options','-enable','1'],capture_output=True,text=True)
  elif enable is False and scope=='cluster':
    subprocess.run(['pvesh','set','/cluster/firewall/options','-enable','0'],capture_output=True,text=True)
except Exception as e:
  lines.append(f'enable_warn:{e}')
# execute provided commands (pvesh/iptables-safe ops as specified by plan)
for c in cmds:
  if not isinstance(c,str) or not c.strip():
    continue
  r=subprocess.run(['bash','-lc',c],capture_output=True,text=True)
  lines.append(f'$ {c}\n{(r.stdout+r.stderr).strip()}')
  if r.returncode!=0:
    print('\n'.join(lines))
    sys.exit(r.returncode)
# basic rule application via proxmox_add_firewall_rule not available locally; prefer commands from plan
if rules and not cmds:
  lines.append('rules_provided_but_no_commands: provide pvesh commands for deterministic apply')
print('\n'.join(lines) or 'apply_firewall_done')
PY
      2>&1) || ok=false ;;
    configure_network)
      net_spec=$(echo "$action_json" | python3 -c 'import sys,json; print(json.dumps(json.load(sys.stdin).get("params") or {}))')
      out=$(python3 - "$net_spec" <<'PY'
import json,subprocess,sys,os,datetime
p=json.loads(sys.argv[1])
dry=bool(p.get('dry_run', True))
snippet=p.get('interfaces_snippet') or ''
cmds=p.get('commands') or []
apply_ifreload=bool(p.get('apply_ifreload', False))
lines=[]
if dry:
  lines.append('DRY_RUN=1')
  if snippet: lines.append('--- interfaces snippet ---\n'+snippet)
  for c in cmds: lines.append('CMD: '+str(c))
  print('\n'.join(lines) or 'dry_run_noop')
  sys.exit(0)
if snippet:
  path='/etc/network/interfaces.d/omni-nexus.cfg'
  ts=datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')
  if os.path.exists(path):
    subprocess.run(['cp','-a',path,f'{path}.bak.{ts}'],capture_output=True,text=True)
  with open(path,'w') as f: f.write(snippet if snippet.endswith('\n') else snippet+'\n')
  lines.append(f'wrote:{path}')
for c in cmds:
  if not isinstance(c,str) or not c.strip(): continue
  r=subprocess.run(['bash','-lc',c],capture_output=True,text=True)
  lines.append(f'$ {c}\n{(r.stdout+r.stderr).strip()}')
  if r.returncode!=0:
    print('\n'.join(lines)); sys.exit(r.returncode)
if apply_ifreload:
  r=subprocess.run(['bash','-lc','ifreload -a 2>&1 || systemctl reload networking 2>&1 || true'],capture_output=True,text=True)
  lines.append((r.stdout+r.stderr).strip())
print('\n'.join(lines) or 'configure_network_done')
PY
      2>&1) || ok=false ;;
    *) ok=false; err="unsupported_action_type:${type}" ;;
  esac
  rc=$?
  set -e
  if [ $rc -ne 0 ] && [ "$ok" = "true" ]; then ok=false; err="exit:$rc"; fi
  send_result "$id" "$ok" "$out" "$err"
}

poll_actions() {
  local resp
  resp=$(api_get "${POLL_ENDPOINT}?node=${NODE_NAME}" 2>/dev/null || echo '{"actions":[]}')
  echo "$resp" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get("actions") or []))' 2>/dev/null | python3 -c '
import sys,json,subprocess,os,tempfile
actions=json.load(sys.stdin)
for a in actions:
  # write each action to temp file and call executor via env? easier: print lines
  print(json.dumps(a,separators=(",",":")))
' 2>/dev/null | while IFS= read -r line; do
    [ -n "$line" ] || continue
    execute_action "$line" || true
  done
}

daemon_loop() {
  [ -n "$MIDDLEWARE_URL" ] || { log "MIDDLEWARE_URL not set in $CONF_FILE"; exit 1; }
  [ -n "$AGENT_TOKEN" ] || { log "AGENT_TOKEN not set in $CONF_FILE"; exit 1; }
  log "starting daemon node=${NODE_NAME} middleware=${MIDDLEWARE_URL} heartbeat=${INTERVAL_SECONDS}s poll=${POLL_INTERVAL_SECONDS}s actions=${ENABLE_ACTIONS}"
  local last_report=0 last_poll=0 now
  while true; do
    now=$(date +%s)
    if [ $((now-last_report)) -ge "$INTERVAL_SECONDS" ]; then send_report >/dev/null 2>&1 && log "heartbeat sent" || log "heartbeat failed"; last_report=$now; fi
    if [ $((now-last_poll)) -ge "$POLL_INTERVAL_SECONDS" ]; then poll_actions >/dev/null 2>&1 || true; last_poll=$now; fi
    sleep 5
  done
}

case "${1:-daemon}" in
  once) send_report && log "heartbeat sent for ${NODE_NAME}" ;;
  poll) poll_actions ;;
  daemon) daemon_loop ;;
  *) echo "Usage: $0 [once|poll|daemon]" >&2; exit 2 ;;
esac
