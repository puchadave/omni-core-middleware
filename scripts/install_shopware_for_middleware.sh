#!/usr/bin/env bash
# One-Click Shopware 6 installer (shopware-cli) preconfigured for OMNI-CORE Middleware
# Modes:
#   --proxmox-lxc        Run on Proxmox VE host: create Debian 12 LXC (amd64, storage local) and install inside
#   (default/--local)    Run inside Debian 12 / Ubuntu (amd64) container/host
# Idempotent best-effort. Secrets/keys are generated at runtime and never hardcoded.
set -euo pipefail

VERSION="1.0.0"
MODE="local"
# Proxmox/LXC defaults
CTID=""; HOSTNAME="shopware-sw6"; CORES=4; MEMORY=4096; SWAP=1024; DISK_GB=20
BRIDGE="vmbr0"; STORAGE="local"; TEMPLATE_STORAGE="local"; TEMPLATE=""
IP_ADDRESS="dhcp"; GATEWAY=""; DNS="1.1.1.1 8.8.8.8"
# Shopware/app defaults
SHOPWARE_DIR="/var/www/shopware"
DB_NAME="shopware"; DB_USER="shopware"; DB_HOST="127.0.0.1"; DB_PORT="3306"
DB_PASS=""; SHOPWARE_URL=""; ADMIN_USER="admin"; ADMIN_EMAIL="admin@example.com"
ADMIN_PASSWORD=""; SHOP_NAME="OMNI-CORE Shop"; LOCALE="de-DE"; CURRENCY="EUR"; COUNTRY="DE"
MIDDLEWARE_IP="127.0.0.1"; MIDDLEWARE_PORT="8002"
WEBHOOK_BASE=""; CREDENTIALS_OUT="/root/omnicore-shopware-credentials.env"
DRY_RUN=0; FORCE=0

log()  { echo "[INFO] $*"; }
ok()   { echo "[OK]   $*"; }
warn() { echo "[WARN] $*"; }
err()  { echo "[ERROR] $*" >&2; }
die()  { err "$*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage(){ cat <<'EOF'
Usage: scripts/install_shopware_for_middleware.sh [options]

Modes:
  --local              Install directly on this Debian/Ubuntu system (default)
  --proxmox-lxc        Run on Proxmox VE host: create Debian 12 LXC and install inside

Shopware / OMNI-CORE:
  --shopware-dir PATH       Install path (default: /var/www/shopware)
  --shop-url URL            Public/base URL (default: http://<host-ip>)
  --admin-user USER         Admin user (default: admin)
  --admin-email EMAIL       Admin email (default: admin@example.com)
  --admin-password PASS     Admin password (generated if empty)
  --db-name NAME            DB name (default: shopware)
  --db-user USER            DB user (default: shopware)
  --db-pass PASS            DB password (generated if empty)
  --middleware-ip IP        OMNI-CORE middleware IP/host (default: 127.0.0.1)
  --middleware-port PORT    OMNI-CORE middleware port (default: 8002)
  --webhook-base URL        Override webhook base (default: http://<middleware-ip>:<port>/api/webhooks/shopware)
  --credentials-out FILE    Output env file (default: /root/omnicore-shopware-credentials.env)

Proxmox (with --proxmox-lxc):
  --ctid ID --hostname NAME --cores N --memory MB --swap MB --disk-gb N
  --bridge BRIDGE --storage NAME --ip CIDR|dhcp --gateway IP --dns "SERVERS"

General:
  --dry-run   Print plan only (no changes)
  --force     Allow reconfigure of existing container/install
  -h, --help  Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --local) MODE="local"; shift;;
    --proxmox-lxc) MODE="proxmox"; shift;;
    --ctid) CTID="${2:-}"; shift 2;;
    --hostname) HOSTNAME="${2:-}"; shift 2;;
    --cores) CORES="${2:-}"; shift 2;;
    --memory) MEMORY="${2:-}"; shift 2;;
    --swap) SWAP="${2:-}"; shift 2;;
    --disk-gb) DISK_GB="${2:-}"; shift 2;;
    --bridge) BRIDGE="${2:-}"; shift 2;;
    --storage) STORAGE="${2:-}"; shift 2;;
    --template-storage) TEMPLATE_STORAGE="${2:-}"; shift 2;;
    --template) TEMPLATE="${2:-}"; shift 2;;
    --ip) IP_ADDRESS="${2:-}"; shift 2;;
    --gateway) GATEWAY="${2:-}"; shift 2;;
    --dns) DNS="${2:-}"; shift 2;;
    --shopware-dir) SHOPWARE_DIR="${2:-}"; shift 2;;
    --shop-url) SHOPWARE_URL="${2:-}"; shift 2;;
    --admin-user) ADMIN_USER="${2:-}"; shift 2;;
    --admin-email) ADMIN_EMAIL="${2:-}"; shift 2;;
    --admin-password) ADMIN_PASSWORD="${2:-}"; shift 2;;
    --db-name) DB_NAME="${2:-}"; shift 2;;
    --db-user) DB_USER="${2:-}"; shift 2;;
    --db-pass) DB_PASS="${2:-}"; shift 2;;
    --middleware-ip) MIDDLEWARE_IP="${2:-}"; shift 2;;
    --middleware-port) MIDDLEWARE_PORT="${2:-}"; shift 2;;
    --webhook-base) WEBHOOK_BASE="${2:-}"; shift 2;;
    --credentials-out) CREDENTIALS_OUT="${2:-}"; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    --force) FORCE=1; shift;;
    -h|--help) usage; exit 0;;
    *) die "Unknown argument: $1";;
  esac
done

[[ -z "$WEBHOOK_BASE" ]] && WEBHOOK_BASE="http://${MIDDLEWARE_IP}:${MIDDLEWARE_PORT}/api/webhooks/shopware"

rand_hex(){ openssl rand -hex "${1:-16}"; }
rand_pw(){ openssl rand -base64 24 | tr -d '\n=/+' | cut -c1-24; }

# -------------------- PROXMOX MODE --------------------
next_free_ctid(){
  local used next=100
  if have pvesh; then
    used=$(pvesh get /cluster/resources --type vm --output-format json 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print("\n".join(str(x.get("vmid")) for x in d if x.get("vmid")))') || true
  fi
  while echo "$used" | grep -qx "$next"; do next=$((next+1)); done
  echo "$next"
}
detect_template(){
  local tmpl
  pveam update >/dev/null 2>&1 || true
  tmpl=$(pveam available -section system 2>/dev/null | awk '$2 ~ /debian-12-standard_.*_amd64\.tar\.(zst|xz|gz)$/ {print $2}' | tail -n1)
  [[ -n "$tmpl" ]] || tmpl=$(pveam available -section system 2>/dev/null | awk '$2 ~ /debian-12-standard_/ {print $2}' | tail -n1)
  echo "$tmpl"
}

run_proxmox_mode(){
  [[ "$(id -u)" -eq 0 ]] || die "Run --proxmox-lxc as root on the Proxmox VE host."
  have pct || die "pct not found. Run on a Proxmox VE host."
  [[ -d /etc/pve ]] || die "/etc/pve not found."
  [[ -z "$CTID" ]] && CTID="$(next_free_ctid)"
  [[ -z "$TEMPLATE" ]] && TEMPLATE="$(detect_template)"
  [[ -n "$TEMPLATE" ]] || die "No Debian 12 amd64 template found. Run: pveam update"
  local net0="name=eth0,bridge=${BRIDGE},ip=${IP_ADDRESS}"
  [[ "$IP_ADDRESS" != "dhcp" && -n "$GATEWAY" ]] && net0+=",gw=${GATEWAY}"
  local tpath="${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}"

  echo "=== PLAN (PROXMOX LXC -> SHOPWARE 6 + OMNI-CORE PRECONFIG) ==="
  cat <<EOF
CTID: $CTID  Hostname: $HOSTNAME  Arch: amd64
Resources: ${CORES} cores / ${MEMORY}MB RAM / ${SWAP}MB swap / ${DISK_GB}G disk
Storage: $STORAGE  Template: $tpath  Bridge: $BRIDGE  IP: $IP_ADDRESS
Shopware dir: $SHOPWARE_DIR  Locale/Currency: $LOCALE/$CURRENCY
Middleware webhook: $WEBHOOK_BASE
Credentials out (in CT): $CREDENTIALS_OUT
Mode: $([[ $DRY_RUN -eq 1 ]] && echo DRY-RUN || echo APPLY)
EOF
  [[ $DRY_RUN -eq 1 ]] && { ok "DRY RUN complete (no changes)."; exit 0; }

  if pct status "$CTID" >/dev/null 2>&1; then
    [[ $FORCE -eq 1 ]] || die "CTID $CTID exists. Use --force to reconfigure/redeploy."
    warn "CTID $CTID exists; reusing container (--force)."
  else
    log "Downloading template and creating LXC $CTID"
    pveam download "$TEMPLATE_STORAGE" "$TEMPLATE" >/dev/null 2>&1 || true
    pct create "$CTID" "$tpath" --hostname "$HOSTNAME" --cores "$CORES" --memory "$MEMORY" --swap "$SWAP" \
      --rootfs "${STORAGE}:${DISK_GB}" --net0 "$net0" --nameserver "$DNS" \
      --onboot 1 --unprivileged 1 --features nesting=1,keyctl=1 --ostype debian --start 0
  fi
  pct start "$CTID" >/dev/null 2>&1 || true
  sleep 6

  log "Pushing installer into CT and executing local install"
  local self; self="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
  pct push "$CTID" "$self" /root/install_shopware_for_middleware.sh >/dev/null
  pct exec "$CTID" -- bash -lc 'chmod +x /root/install_shopware_for_middleware.sh'
  local inner=(/root/install_shopware_for_middleware.sh --local \
    --shopware-dir "$SHOPWARE_DIR" --admin-user "$ADMIN_USER" --admin-email "$ADMIN_EMAIL" \
    --db-name "$DB_NAME" --db-user "$DB_USER" --middleware-ip "$MIDDLEWARE_IP" --middleware-port "$MIDDLEWARE_PORT" \
    --webhook-base "$WEBHOOK_BASE" --credentials-out "$CREDENTIALS_OUT" --locale "$LOCALE" --currency "$CURRENCY")
  [[ -n "$SHOPWARE_URL" ]] && inner+=(--shop-url "$SHOPWARE_URL")
  [[ -n "$ADMIN_PASSWORD" ]] && inner+=(--admin-password "$ADMIN_PASSWORD")
  [[ -n "$DB_PASS" ]] && inner+=(--db-pass "$DB_PASS")
  [[ $FORCE -eq 1 ]] && inner+=(--force)
  # shellcheck disable=SC2145
  pct exec "$CTID" -- bash -lc "$(printf '%q ' "${inner[@]}")"

  local ctip; ctip=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}' || echo "<ct-ip>")
  ok "Proxmox LXC provisioning finished. CTID=$CTID IP=${ctip}"
  echo "Credentials: pct exec $CTID -- cat $CREDENTIALS_OUT"
  return 0
}

# -------------------- LOCAL MODE --------------------
require_debian_like(){
  [[ -f /etc/os-release ]] || die "Cannot detect OS (/etc/os-release missing)."
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in debian|ubuntu) ;; *) warn "OS '$ID' not Debian/Ubuntu; continuing best-effort.";; esac
  [[ "$(uname -m)" == "x86_64" || "$(uname -m)" == "amd64" ]] || warn "Architecture $(uname -m) is not amd64; shopware-cli repo targets amd64."
}

mysql_exec(){ mysql -NBe "$1"; }
mysql_db(){ mysql -D "$DB_NAME" -NBe "$1"; }

ensure_packages(){
  log "Installing base packages, Nginx, PHP 8.3, MariaDB, Composer, Node.js"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y ca-certificates curl gnupg lsb-release apt-transport-https sudo unzip git jq mariadb-server \
    nginx composer nodejs npm

  # PHP 8.3 repo (Sury for Debian 12 compatibility)
  if ! apt-cache policy php8.3-fpm 2>/dev/null | grep -q Candidate; then
    . /etc/os-release
    if [[ "${ID:-}" == "debian" ]]; then
      curl -fsSL https://packages.sury.org/php/apt.gpg | gpg --dearmor -o /usr/share/keyrings/sury-php.gpg
      echo "deb [signed-by=/usr/share/keyrings/sury-php.gpg] https://packages.sury.org/php/ $(lsb_release -sc) main" > /etc/apt/sources.list.d/sury-php.list
    else
      add-apt-repository -y ppa:ondrej/php || true
    fi
    apt-get update -y
  fi
  apt-get install -y php8.3-fpm php8.3-cli php8.3-intl php8.3-gd php8.3-mbstring php8.3-xml php8.3-zip php8.3-curl \
    php8.3-bcmath php8.3-mysql php8.3-redis php8.3-sodium php8.3-opcache || \
  apt-get install -y php-fpm php-cli php-intl php-gd php-mbstring php-xml php-zip php-curl php-bcmath php-mysql php-redis php-sodium php-opcache

  # shopware-cli (FriendsOfShopware stable)
  if ! have shopware-cli; then
    log "Installing shopware-cli from FriendsOfShopware Cloudsmith repo"
    curl -1sLf 'https://dl.cloudsmith.io/public/friendsofshopware/stable/setup.deb.sh' | bash
    apt-get update -y
    apt-get install -y shopware-cli
  else
    ok "shopware-cli already installed"
  fi
  ok "System packages ready"
}

tune_stack(){
  log "Configuring PHP, Nginx and MariaDB"
  local phpini fpm_sock
  phpini=$(php -r 'echo PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;' 2>/dev/null || echo "8.3")
  # PHP tuning
  for ini in /etc/php/${phpini}/fpm/php.ini /etc/php/${phpini}/cli/php.ini; do
    [[ -f "$ini" ]] || continue
    sed -i 's/^memory_limit = .*/memory_limit = 512M/; s/^max_execution_time = .*/max_execution_time = 300/; s/^upload_max_filesize = .*/upload_max_filesize = 128M/; s/^post_max_size = .*/post_max_size = 128M/' "$ini" || true
    grep -q '^opcache.enable' "$ini" || echo 'opcache.enable=1' >> "$ini"
  done
  systemctl enable --now "php${phpini}-fpm" 2>/dev/null || systemctl enable --now php-fpm 2>/dev/null || true

  # MariaDB tuning (light, idempotent)
  mkdir -p /etc/mysql/mariadb.conf.d
  cat >/etc/mysql/mariadb.conf.d/99-omni-shopware.cnf <<'CNF'
[mysqld]
innodb_buffer_pool_size=512M
innodb_log_file_size=128M
innodb_flush_log_at_trx_commit=2
innodb_flush_method=O_DIRECT
max_allowed_packet=128M
CNF
  systemctl enable --now mariadb 2>/dev/null || systemctl enable --now mysql 2>/dev/null || true

  # Nginx vhost
  fpm_sock="/run/php/php${phpini}-fpm.sock"; [[ -S "$fpm_sock" ]] || fpm_sock=$(ls /run/php/php*-fpm.sock 2>/dev/null | head -n1 || echo /run/php/php-fpm.sock)
  cat >/etc/nginx/sites-available/shopware.conf <<NGX
server {
    listen 80;
    server_name _;
    root ${SHOPWARE_DIR}/public;
    index index.php index.html;
    client_max_body_size 128M;

    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml application/xml+rss text/javascript;

    location /recovery/update/ { try_files \$uri /recovery/update/index.php\$is_args\$args; }
    location /recovery/install/ { try_files \$uri /recovery/install/index.php\$is_args\$args; }

    location / {
        try_files \$uri /index.php\$is_args\$args;
    }

    location ~ \.php$ {
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:${fpm_sock};
        fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
        fastcgi_buffers 16 16k;
        fastcgi_buffer_size 32k;
        fastcgi_read_timeout 300;
        include fastcgi_params;
    }

    location ~* \.(?:css|js|jpg|jpeg|gif|png|ico|svg|webp|woff2?)$ {
        expires 7d;
        access_log off;
        add_header Cache-Control "public";
    }
}
NGX
  ln -sf /etc/nginx/sites-available/shopware.conf /etc/nginx/sites-enabled/shopware.conf
  rm -f /etc/nginx/sites-enabled/default || true
  nginx -t
  systemctl enable --now nginx
  ok "Stack configured (PHP/Nginx/MariaDB)"
}

ensure_database(){
  log "Ensuring MariaDB database and user"
  [[ -n "$DB_PASS" ]] || DB_PASS="$(rand_pw)"
  systemctl is-active --quiet mariadb || systemctl start mariadb || systemctl start mysql || true
  mysql_exec "CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" || true
  mysql_exec "CREATE USER IF NOT EXISTS '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASS}';" || true
  mysql_exec "CREATE USER IF NOT EXISTS '${DB_USER}'@'127.0.0.1' IDENTIFIED BY '${DB_PASS}';" || true
  mysql_exec "ALTER USER '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASS}';" || true
  mysql_exec "ALTER USER '${DB_USER}'@'127.0.0.1' IDENTIFIED BY '${DB_PASS}';" || true
  mysql_exec "GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'localhost';" || true
  mysql_exec "GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'127.0.0.1';" || true
  mysql_exec "FLUSH PRIVILEGES;" || true
  ok "Database '${DB_NAME}' and user '${DB_USER}' ready"
}

install_shopware(){
  log "Provisioning Shopware 6 via shopware-cli / bin/console"
  mkdir -p "$SHOPWARE_DIR"
  [[ -n "$ADMIN_PASSWORD" ]] || ADMIN_PASSWORD="$(rand_pw)"

  local host_ip; host_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo 127.0.0.1)
  [[ -n "$SHOPWARE_URL" ]] || SHOPWARE_URL="http://${host_ip}"

  if [[ ! -f "$SHOPWARE_DIR/bin/console" ]]; then
    if have shopware-cli; then
      log "Creating Shopware project with shopware-cli (production template)"
      # try common shopware-cli forms; fall back to composer create-project
      (cd "$(dirname "$SHOPWARE_DIR")" && shopware-cli project create "$(basename "$SHOPWARE_DIR")" --no-interaction) || \
      (cd "$(dirname "$SHOPWARE_DIR")" && shopware-cli project create shopware "$(basename "$SHOPWARE_DIR")") || \
      composer create-project shopware/production "$SHOPWARE_DIR" --no-interaction || true
    else
      composer create-project shopware/production "$SHOPWARE_DIR" --no-interaction
    fi
  else
    ok "Shopware project already present at $SHOPWARE_DIR (idempotent)"
  fi
  [[ -f "$SHOPWARE_DIR/bin/console" ]] || die "Shopware console not found at $SHOPWARE_DIR/bin/console"

  local dburl="mysql://${DB_USER}:${DB_PASS}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
  # .env / APP_URL
  if [[ -f "$SHOPWARE_DIR/.env" ]]; then
    grep -q '^DATABASE_URL=' "$SHOPWARE_DIR/.env" && sed -i "s|^DATABASE_URL=.*|DATABASE_URL=\"${dburl}\"|" "$SHOPWARE_DIR/.env" || echo "DATABASE_URL=\"${dburl}\"" >> "$SHOPWARE_DIR/.env"
    grep -q '^APP_URL=' "$SHOPWARE_DIR/.env" && sed -i "s|^APP_URL=.*|APP_URL=\"${SHOPWARE_URL}\"|" "$SHOPWARE_DIR/.env" || echo "APP_URL=\"${SHOPWARE_URL}\"" >> "$SHOPWARE_DIR/.env"
    grep -q '^APP_ENV=' "$SHOPWARE_DIR/.env" && sed -i "s|^APP_ENV=.*|APP_ENV=prod|" "$SHOPWARE_DIR/.env" || echo "APP_ENV=prod" >> "$SHOPWARE_DIR/.env"
  else
    cat >"$SHOPWARE_DIR/.env" <<ENV
APP_ENV=prod
APP_URL="${SHOPWARE_URL}"
DATABASE_URL="${dburl}"
APP_SECRET="$(rand_hex 32)"
BLUE_GREEN_DEPLOYMENT=0
ENV
  fi

  log "Running Shopware system install / basic setup (de-DE, EUR, DE)"
  (cd "$SHOPWARE_DIR" && php bin/console system:install --create-database --basic-setup \
      --shop-name="$SHOP_NAME" --shop-locale="$LOCALE" --shop-currency="$CURRENCY" \
      --shop-email="$ADMIN_EMAIL" --force 2>&1 || php bin/console system:install --create-database --basic-setup --force 2>&1 || true)

  # ensure admin user (idempotent)
  (cd "$SHOPWARE_DIR" && php bin/console user:create "$ADMIN_USER" --admin --email="$ADMIN_EMAIL" --password="$ADMIN_PASSWORD" --firstName=OMNI --lastName=Admin 2>&1 || true)

  # disable first-run-wizard
  mysql_db "UPDATE system_config SET configuration_value = '{\"_value\": true}' WHERE configuration_key = 'core.frw.completedAt' LIMIT 1;" 2>/dev/null || true
  (cd "$SHOPWARE_DIR" && php bin/console system:config:set core.frw.completedAt "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" 2>&1 || true)

  chown -R www-data:www-data "$SHOPWARE_DIR" || true
  (cd "$SHOPWARE_DIR" && php bin/console cache:clear 2>&1 || true)
  ok "Shopware installed/configured at $SHOPWARE_DIR"
}

hex_uuid(){ python3 - <<'PY'
import uuid; print(uuid.uuid4().hex)
PY
}

configure_omnicore_integration(){
  log "Configuring OMNI-CORE integration, IDs and webhooks"
  local access_key="SWIA$(tr -dc 'A-Z0-9' </dev/urandom | head -c 20)"
  local secret_key
  # Shopware stores secret_access_key in plain in integration table (used with HMAC by clients) in many setups; keep runtime-generated
  secret_key="$(rand_hex 32)"
  local iid ts
  iid="$(hex_uuid)"
  ts="$(date -u +%Y-%m-%d\ %H:%M:%S.%3N)"

  # integration (idempotent by label)
  local existing
  existing=$(mysql_db "SELECT LOWER(HEX(id)) FROM integration WHERE label='OMNI-CORE Middleware' LIMIT 1;" 2>/dev/null || echo "")
  if [[ -n "$existing" ]]; then
    iid="$existing"
    mysql_db "UPDATE integration SET access_key='${access_key}', secret_access_key='${secret_key}', admin=1, active=1, updated_at='${ts}' WHERE id=UNHEX('${iid}');" 2>/dev/null || true
    ok "Integration 'OMNI-CORE Middleware' updated"
  else
    mysql_db "INSERT INTO integration (id, access_key, secret_access_key, label, admin, active, created_at) VALUES (UNHEX('${iid}'),'${access_key}','${secret_key}','OMNI-CORE Middleware',1,1,'${ts}');" 2>/dev/null || \
      warn "Could not insert integration row (schema may differ); configure integration manually in Admin if needed."
  fi

  # read IDs (best-effort)
  local sales_channel_id tax_id currency_id root_category_id
  sales_channel_id=$(mysql_db "SELECT LOWER(HEX(id)) FROM sales_channel WHERE type_id IN (SELECT id FROM sales_channel_type WHERE name LIKE '%Storefront%' OR name LIKE '%Store%' LIMIT 1) AND active=1 LIMIT 1;" 2>/dev/null || echo "")
  [[ -z "$sales_channel_id" ]] && sales_channel_id=$(mysql_db "SELECT LOWER(HEX(id)) FROM sales_channel WHERE active=1 LIMIT 1;" 2>/dev/null || echo "")
  tax_id=$(mysql_db "SELECT LOWER(HEX(id)) FROM tax WHERE tax_rate=19.00 LIMIT 1;" 2>/dev/null || echo "")
  [[ -z "$tax_id" ]] && tax_id=$(mysql_db "SELECT LOWER(HEX(id)) FROM tax ORDER BY tax_rate DESC LIMIT 1;" 2>/dev/null || echo "")
  currency_id=$(mysql_db "SELECT LOWER(HEX(id)) FROM currency WHERE iso_code='EUR' LIMIT 1;" 2>/dev/null || echo "")
  root_category_id=$(mysql_db "SELECT LOWER(HEX(navigation_category_id)) FROM sales_channel WHERE id=UNHEX('${sales_channel_id}') LIMIT 1;" 2>/dev/null || echo "")
  [[ -z "$root_category_id" ]] && root_category_id=$(mysql_db "SELECT LOWER(HEX(id)) FROM category WHERE parent_id IS NULL LIMIT 1;" 2>/dev/null || echo "")

  # webhooks (order.placed, state_enter.order_transaction.state.paid, product.written)
  local ev wid
  for ev in checkout.order.placed state_enter.order_transaction.state.paid product.written; do
    local ex
    ex=$(mysql_db "SELECT LOWER(HEX(id)) FROM webhook WHERE name='omnicore-${ev}' LIMIT 1;" 2>/dev/null || echo "")
    if [[ -n "$ex" ]]; then
      mysql_db "UPDATE webhook SET url='${WEBHOOK_BASE}', event_name='${ev}', active=1, updated_at='${ts}' WHERE id=UNHEX('${ex}');" 2>/dev/null || true
    else
      wid="$(hex_uuid)"
      mysql_db "INSERT INTO webhook (id, name, event_name, url, active, created_at) VALUES (UNHEX('${wid}'),'omnicore-${ev}','${ev}','${WEBHOOK_BASE}',1,'${ts}');" 2>/dev/null || \
        warn "Could not register webhook '${ev}' (schema/app-system differences); register manually if needed."
    fi
  done
  ok "Integration keys/IDs/webhooks configured (best-effort, idempotent)"

  export SW_CLIENT_ID="$access_key" SW_CLIENT_SECRET="$secret_key" \
         SW_SALES_CHANNEL_ID="$sales_channel_id" SW_DEFAULT_TAX_ID="$tax_id" \
         SW_ROOT_CATEGORY_ID="$root_category_id" SW_CURRENCY_ID="$currency_id"
}

setup_workers(){
  log "Setting up systemd workers (messenger consumer, scheduled tasks)"
  cat >/etc/systemd/system/shopware-consumer.service <<UNIT
[Unit]
Description=Shopware Messenger Consumer (OMNI-CORE)
After=network.target mariadb.service
[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=${SHOPWARE_DIR}
ExecStart=/usr/bin/php ${SHOPWARE_DIR}/bin/console messenger:consume async failed --time-limit=60 --memory-limit=256M
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
  cat >/etc/systemd/system/shopware-scheduled-task.service <<UNIT
[Unit]
Description=Shopware Scheduled Task Runner (OMNI-CORE)
After=network.target mariadb.service
[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=${SHOPWARE_DIR}
ExecStart=/usr/bin/php ${SHOPWARE_DIR}/bin/console scheduled-task:run --time-limit=60 --memory-limit=256M
Restart=always
RestartSec=60
[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now shopware-consumer.service shopware-scheduled-task.service || warn "Could not enable/start worker services (check systemd)."
  ok "Worker services configured"
}

write_credentials(){
  log "Writing OMNI-CORE credentials and summary"
  mkdir -p "$(dirname "$CREDENTIALS_OUT")"
  local host_ip; host_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo 127.0.0.1)
  [[ -n "$SHOPWARE_URL" ]] || SHOPWARE_URL="http://${host_ip}"
  umask 077
  cat >"$CREDENTIALS_OUT" <<ENV
# OMNI-CORE Middleware - Shopware 6 integration (generated)
SHOPWARE_URL=${SHOPWARE_URL}
SHOPWARE_CLIENT_ID=${SW_CLIENT_ID:-}
SHOPWARE_CLIENT_SECRET=${SW_CLIENT_SECRET:-}
SHOPWARE_SALES_CHANNEL_ID=${SW_SALES_CHANNEL_ID:-}
SHOPWARE_DEFAULT_TAX_ID=${SW_DEFAULT_TAX_ID:-}
SHOPWARE_ROOT_CATEGORY_ID=${SW_ROOT_CATEGORY_ID:-}
# Additional
SHOPWARE_CURRENCY_ID=${SW_CURRENCY_ID:-}
SHOPWARE_ADMIN_USER=${ADMIN_USER}
SHOPWARE_ADMIN_EMAIL=${ADMIN_EMAIL}
ENV
  chmod 600 "$CREDENTIALS_OUT" || true

  echo
  echo "================ SHOPWARE 6 (OMNI-CORE READY) ================"
  echo "Shop URL:        ${SHOPWARE_URL}"
  echo "Admin URL:       ${SHOPWARE_URL}/admin"
  echo "Admin login:     ${ADMIN_USER} / ${ADMIN_EMAIL}  (password set during install; stored only if you passed/kept it securely)"
  echo "Middleware hook: ${WEBHOOK_BASE}"
  echo "Credentials:     ${CREDENTIALS_OUT}"
  echo
  echo "# .env block for OMNI-CORE Middleware"
  cat "$CREDENTIALS_OUT"
  echo "=============================================================="
  ok "Completed. Verify API access and webhooks from OMNI-CORE."
}

run_local_install(){
  [[ "$(id -u)" -eq 0 ]] || die "Run local install as root."
  require_debian_like
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "=== PLAN (LOCAL SHOPWARE 6 + OMNI-CORE PRECONFIG) ==="
    echo "Dir: $SHOPWARE_DIR  DB: $DB_NAME/$DB_USER  URL: ${SHOPWARE_URL:-auto}  Webhook: $WEBHOOK_BASE  Out: $CREDENTIALS_OUT"
    ok "DRY RUN complete (no changes)."; exit 0
  fi
  ensure_packages
  tune_stack
  ensure_database
  install_shopware
  configure_omnicore_integration
  setup_workers
  write_credentials
}

main(){
  echo "[INFO] OMNI-CORE Shopware installer v${VERSION} (mode=${MODE})"
  if [[ "$MODE" == "proxmox" ]]; then run_proxmox_mode; else run_local_install; fi
}

main "$@"
