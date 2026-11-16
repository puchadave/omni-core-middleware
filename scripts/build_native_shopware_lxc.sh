#!/usr/bin/env bash
#
# build_native_shopware_lxc.sh
# Build a native Alpine amd64 LXC running Shopware 6 + MariaDB + PHP-FPM + Nginx via OpenRC.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib_nexus_proxmox.sh
source "${SCRIPT_DIR}/lib_nexus_proxmox.sh"

TARGET_NODE="LOCAL"
SSH_USER="root"
CTID="AUTO"
IP_SPEC="dhcp"
STORAGE="local"
NET_BRIDGE="vmbr0"
HOSTNAME="shopware-server"
CORES="4"
MEMORY="4096"
DISK_GB="20"
SHOPWARE_DIR="/var/www/shopware"
DB_NAME="shopware"
DB_USER="shopware"
DB_PASS=""
SHOPWARE_URL=""
ADMIN_USER="admin"
ADMIN_EMAIL="admin@example.com"
ADMIN_PASSWORD=""
SHOP_NAME="OMNI-CORE Shop"
LOCALE="de-DE"
CURRENCY="EUR"
COUNTRY="DE"
MIDDLEWARE_IP=""
MIDDLEWARE_PORT="8002"
WEBHOOK_BASE=""
JSON_OUT=""

usage() {
    cat <<'EOF'
Usage: build_native_shopware_lxc.sh [options]
  --target-node LOCAL|SSH_HOST  Proxmox target (default: LOCAL)
  --ssh-user USER               SSH user for remote target (default: root)
  --ctid AUTO|ID                Container ID (default: AUTO)
  --ip dhcp|CIDR,gw=GW          Container network config (default: dhcp)
  --storage NAME                Proxmox storage for rootfs (default: local)
  --netbridge BRIDGE            Linux bridge (default: vmbr0)
  --hostname NAME               LXC hostname (default: shopware-server)
  --cores N                     CPU cores (default: 4)
  --memory MB                   RAM MB (default: 4096)
  --disk GB                     Rootfs GB on local (default: 20)
  --shopware-dir PATH           Shopware install path (default /var/www/shopware)
  --db-name NAME                MariaDB database (default shopware)
  --db-user USER                MariaDB user (default shopware)
  --db-pass PASS                MariaDB password (generated if empty)
  --shop-url URL                Public Shopware URL (default http://<container-ip>)
  --admin-user USER             Admin user (default admin)
  --admin-email EMAIL           Admin email
  --admin-password PASS         Admin password (generated if empty)
  --shop-name NAME              Shop name
  --locale LOCALE               Locale (default de-DE)
  --currency CURRENCY           Currency (default EUR)
  --country CODE                Country (default DE)
  --middleware-ip IP            Optional OMNI-CORE middleware IP for integration/webhooks
  --middleware-port PORT        OMNI-CORE middleware port (default 8002)
  --webhook-base URL            Override webhook base URL
  --json-out PATH               Write machine-readable JSON result to PATH
  -h, --help                    Show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --target-node) TARGET_NODE="${2:?}"; shift 2 ;;
        --ssh-user) SSH_USER="${2:?}"; shift 2 ;;
        --ctid) CTID="${2:?}"; shift 2 ;;
        --ip) IP_SPEC="${2:?}"; shift 2 ;;
        --storage) STORAGE="${2:?}"; shift 2 ;;
        --netbridge) NET_BRIDGE="${2:?}"; shift 2 ;;
        --hostname) HOSTNAME="${2:?}"; shift 2 ;;
        --cores) CORES="${2:?}"; shift 2 ;;
        --memory) MEMORY="${2:?}"; shift 2 ;;
        --disk) DISK_GB="${2:?}"; shift 2 ;;
        --shopware-dir) SHOPWARE_DIR="${2:?}"; shift 2 ;;
        --db-name) DB_NAME="${2:?}"; shift 2 ;;
        --db-user) DB_USER="${2:?}"; shift 2 ;;
        --db-pass) DB_PASS="${2:?}"; shift 2 ;;
        --shop-url) SHOPWARE_URL="${2:?}"; shift 2 ;;
        --admin-user) ADMIN_USER="${2:?}"; shift 2 ;;
        --admin-email) ADMIN_EMAIL="${2:?}"; shift 2 ;;
        --admin-password) ADMIN_PASSWORD="${2:?}"; shift 2 ;;
        --shop-name) SHOP_NAME="${2:?}"; shift 2 ;;
        --locale) LOCALE="${2:?}"; shift 2 ;;
        --currency) CURRENCY="${2:?}"; shift 2 ;;
        --country) COUNTRY="${2:?}"; shift 2 ;;
        --middleware-ip) MIDDLEWARE_IP="${2:?}"; shift 2 ;;
        --middleware-port) MIDDLEWARE_PORT="${2:?}"; shift 2 ;;
        --webhook-base) WEBHOOK_BASE="${2:?}"; shift 2 ;;
        --json-out) JSON_OUT="${2:?}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

require_cmd pct
require_cmd pveam
require_cmd jq
CTID="$(resolve_ctid "${CTID}")"
parse_ip_spec "${IP_SPEC}"
[ -n "${DB_PASS}" ] || DB_PASS="$(openssl rand -base64 24 | tr -d '\n=/+' | cut -c1-24)"
[ -n "${ADMIN_PASSWORD}" ] || ADMIN_PASSWORD="$(openssl rand -base64 24 | tr -d '\n=/+' | cut -c1-24)"
[ -n "${SHOPWARE_URL}" ] || SHOPWARE_URL="http://__CT_IP__"
[ -n "${WEBHOOK_BASE}" ] || WEBHOOK_BASE="http://${MIDDLEWARE_IP:-127.0.0.1}:${MIDDLEWARE_PORT}/api/webhooks/shopware"

if ! ct_exists; then
    create_lxc "${HOSTNAME}" "${CORES}" "${MEMORY}" "${DISK_GB}"
else
    log_info "Container ${CTID} already exists; reusing it"
fi
start_and_wait_ct
CT_IP="$(get_ct_ip)"
[ -n "${CT_IP}" ] || die "could not determine container IP"
SHOPWARE_URL="${SHOPWARE_URL/__CT_IP__/${CT_IP}}"
WEBHOOK_BASE="${WEBHOOK_BASE/__CT_IP__/${CT_IP}}"

BUILD_DIR="$(mktemp -d /tmp/shopware-build.XXXXXX)"
trap 'rm -rf "${BUILD_DIR}"' EXIT
cat > "${BUILD_DIR}/build.env" <<ENV
SHOPWARE_DIR=${SHOPWARE_DIR}
DB_NAME=${DB_NAME}
DB_USER=${DB_USER}
DB_PASS=${DB_PASS}
SHOPWARE_URL=${SHOPWARE_URL}
ADMIN_USER=${ADMIN_USER}
ADMIN_EMAIL=${ADMIN_EMAIL}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
SHOP_NAME=${SHOP_NAME}
LOCALE=${LOCALE}
CURRENCY=${CURRENCY}
COUNTRY=${COUNTRY}
MIDDLEWARE_IP=${MIDDLEWARE_IP}
MIDDLEWARE_PORT=${MIDDLEWARE_PORT}
WEBHOOK_BASE=${WEBHOOK_BASE}
ENV

cat > "${BUILD_DIR}/build.sh" <<'BUILD'
#!/usr/bin/env bash
set -euo pipefail
export HOME=/root
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
source /tmp/shopware-build/build.env

if ! grep -q '^community' /etc/apk/repositories; then
    sed -i 's#^\#community#community#' /etc/apk/repositories
fi
apk update
apk add --no-cache bash curl wget git jq ca-certificates tzdata nginx mariadb mariadb-client \
    php83 php83-fpm php83-cli php83-intl php83-gd php83-mbstring php83-xml php83-zip \
    php83-curl php83-bcmath php83-mysqli php83-pdo_mysql php83-redis php83-sodium \
    php83-opcache php83-phar php83-session php83-fileinfo php83-dom php83-simplexml \
    php83-xmlwriter php83-tokenizer php83-ctype php83-openssl composer unzip

PHP_BIN="$(command -v php83 || command -v php)"
PHP_INI_DIR="$("${PHP_BIN}" -i | awk -F' => ' '/^Scan this dir for additional \.ini files/ {print $2; exit}')"
[ -n "${PHP_INI_DIR}" ] || PHP_INI_DIR="/etc/php83/conf.d"
mkdir -p "${PHP_INI_DIR}"
cat > "${PHP_INI_DIR}/99-omni-shopware.ini" <<'INI'
memory_limit=512M
max_execution_time=300
max_input_time=300
post_max_size=64M
upload_max_filesize=64M
date.timezone=UTC
opcache.enable=1
opcache.enable_cli=1
opcache.memory_consumption=256
opcache.interned_strings_buffer=16
opcache.max_accelerated_files=30000
opcache.validate_timestamps=0
opcache.save_comments=1
realpath_cache_size=4096K
realpath_cache_ttl=7200
INI

PHP_FPM_POOL="/etc/php83/php-fpm.d/www.conf"
[ -f "${PHP_FPM_POOL}" ] || PHP_FPM_POOL="/etc/php83/fpm.d/www.conf"
if [ -f "${PHP_FPM_POOL}" ]; then
    sed -i 's/^listen = .*/listen = 127.0.0.1:9000/' "${PHP_FPM_POOL}"
    sed -i 's/^user = .*/user = nginx/' "${PHP_FPM_POOL}"
    sed -i 's/^group = .*/group = nginx/' "${PHP_FPM_POOL}"
    sed -i 's/^pm = .*/pm = dynamic/' "${PHP_FPM_POOL}" || true
    sed -i 's/^pm.max_children = .*/pm.max_children = 20/' "${PHP_FPM_POOL}" || true
    sed -i 's/^pm.start_servers = .*/pm.start_servers = 4/' "${PHP_FPM_POOL}" || true
    sed -i 's/^pm.min_spare_servers = .*/pm.min_spare_servers = 2/' "${PHP_FPM_POOL}" || true
    sed -i 's/^pm.max_spare_servers = .*/pm.max_spare_servers = 8/' "${PHP_FPM_POOL}" || true
    grep -q '^pm.max_requests' "${PHP_FPM_POOL}" && sed -i 's/^pm.max_requests = .*/pm.max_requests = 500/' "${PHP_FPM_POOL}" || echo 'pm.max_requests = 500' >> "${PHP_FPM_POOL}"
fi

if [ ! -d /var/lib/mysql/mysql ]; then
    mariadb-install-db --user=mysql --datadir=/var/lib/mysql --auth-root-authentication-method=normal >/dev/null
fi
mkdir -p /run/mysqld /run/php
chown mysql:mysql /run/mysqld /var/lib/mysql
rc-update add mariadb default 2>/dev/null || rc-update add mysql default 2>/dev/null || true
rc-service mariadb start 2>/dev/null || rc-service mysql start 2>/dev/null || true
for i in $(seq 1 30); do
    mariadb-admin ping >/dev/null 2>&1 && break
    sleep 1
done
mariadb -e "CREATE DATABASE IF NOT EXISTS ${DB_NAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
mariadb -e "CREATE USER IF NOT EXISTS '${DB_USER}'@'127.0.0.1' IDENTIFIED BY '${DB_PASS}'; GRANT ALL PRIVILEGES ON ${DB_NAME}.* TO '${DB_USER}'@'127.0.0.1'; FLUSH PRIVILEGES;"

rc-update add php-fpm83 default 2>/dev/null || rc-update add php-fpm default 2>/dev/null || true
rc-service php-fpm83 start 2>/dev/null || rc-service php-fpm start 2>/dev/null || true

mkdir -p "${SHOPWARE_DIR}"
if [ ! -f "${SHOPWARE_DIR}/bin/console" ]; then
    export COMPOSER_HOME=/tmp/composer
    composer create-project shopware/production "${SHOPWARE_DIR}" --no-interaction --no-dev --optimize-autoloader --remove-vcs
fi
[ -f "${SHOPWARE_DIR}/bin/console" ] || { echo "Shopware console missing" >&2; exit 1; }

APP_SECRET="$(openssl rand -hex 32)"
cat > "${SHOPWARE_DIR}/.env" <<ENV
APP_ENV=prod
APP_URL=${SHOPWARE_URL}
APP_SECRET=${APP_SECRET}
DATABASE_URL="mysql://${DB_USER}:${DB_PASS}@127.0.0.1:3306/${DB_NAME}"
MAILER_DSN=null://null
BLUE_GREEN_DEPLOYMENT=0
SHOPWARE_HTTP_CACHE_ENABLED=0
COMPOSER_HOME=/tmp/composer
ENV
chmod 640 "${SHOPWARE_DIR}/.env"
chown -R nginx:nginx "${SHOPWARE_DIR}"

cd "${SHOPWARE_DIR}"
"${PHP_BIN}" bin/console system:install --create-database --basic-setup \
    --shop-name="${SHOP_NAME}" --shop-locale="${LOCALE}" --shop-currency="${CURRENCY}" \
    --shop-email="${ADMIN_EMAIL}" --force || true
"${PHP_BIN}" bin/console user:create "${ADMIN_USER}" --admin --email="${ADMIN_EMAIL}" \
    --password="${ADMIN_PASSWORD}" --firstName=OMNI --lastName=Admin || true
"${PHP_BIN}" bin/console system:config:set core.frw.completedAt "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" || true
"${PHP_BIN}" bin/console cache:clear || true

if [ -n "${MIDDLEWARE_IP}" ]; then
    ACCESS_KEY="SWIA$(openssl rand -hex 16 | tr '[:lower:]' '[:upper:]')"
    SECRET_KEY="$(openssl rand -hex 32)"
    TS="$(date -u +%Y-%m-%d %H:%M:%S.000)"
    IID="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
    mariadb "${DB_NAME}" -e "INSERT INTO integration (id, access_key, secret_access_key, label, admin, active, created_at) VALUES (UNHEX('${IID}'),'${ACCESS_KEY}','${SECRET_KEY}','OMNI-CORE Middleware',1,1,'${TS}');" 2>/dev/null || true
    SALES_CHANNEL_ID="$(mariadb "${DB_NAME}" -N -e "SELECT LOWER(HEX(id)) FROM sales_channel WHERE active=1 LIMIT 1;" 2>/dev/null | head -n1 || true)"
    TAX_ID="$(mariadb "${DB_NAME}" -N -e "SELECT LOWER(HEX(id)) FROM tax WHERE tax_rate=19.00 LIMIT 1;" 2>/dev/null | head -n1 || true)"
    CURRENCY_ID="$(mariadb "${DB_NAME}" -N -e "SELECT LOWER(HEX(id)) FROM currency WHERE iso_code='EUR' LIMIT 1;" 2>/dev/null | head -n1 || true)"
    ROOT_CATEGORY_ID="$(mariadb "${DB_NAME}" -N -e "SELECT LOWER(HEX(navigation_category_id)) FROM sales_channel WHERE id=UNHEX('${SALES_CHANNEL_ID}') LIMIT 1;" 2>/dev/null | head -n1 || true)"
    for EV in checkout.order.placed state_enter.order_transaction.state.paid product.written; do
        WID="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
        mariadb "${DB_NAME}" -e "INSERT INTO webhook (id, name, event_name, url, active, created_at) VALUES (UNHEX('${WID}'),'omnicore-${EV}','${EV}','${WEBHOOK_BASE}',1,'${TS}');" 2>/dev/null || true
    done
    umask 077
    cat > /root/shopware-credentials.env <<ENV
SHOPWARE_URL=${SHOPWARE_URL}
SHOPWARE_CLIENT_ID=${ACCESS_KEY}
SHOPWARE_CLIENT_SECRET=${SECRET_KEY}
SHOPWARE_SALES_CHANNEL_ID=${SALES_CHANNEL_ID}
SHOPWARE_DEFAULT_TAX_ID=${TAX_ID}
SHOPWARE_ROOT_CATEGORY_ID=${ROOT_CATEGORY_ID}
SHOPWARE_CURRENCY_ID=${CURRENCY_ID}
SHOPWARE_ADMIN_USER=${ADMIN_USER}
SHOPWARE_ADMIN_PASSWORD=${ADMIN_PASSWORD}
SHOPWARE_DB_NAME=${DB_NAME}
SHOPWARE_DB_USER=${DB_USER}
SHOPWARE_DB_PASS=${DB_PASS}
ENV
    chmod 600 /root/shopware-credentials.env
else
    umask 077
    cat > /root/shopware-credentials.env <<ENV
SHOPWARE_URL=${SHOPWARE_URL}
SHOPWARE_ADMIN_USER=${ADMIN_USER}
SHOPWARE_ADMIN_PASSWORD=${ADMIN_PASSWORD}
SHOPWARE_DB_NAME=${DB_NAME}
SHOPWARE_DB_USER=${DB_USER}
SHOPWARE_DB_PASS=${DB_PASS}
ENV
    chmod 600 /root/shopware-credentials.env
fi

cat > /etc/nginx/http.d/shopware.conf <<NGX
server {
    listen 80 default_server;
    server_name _;
    root ${SHOPWARE_DIR}/public;
    index index.php index.html;
    client_max_body_size 64M;
    location /recovery/update/ { try_files \$uri /recovery/update/index.php\$is_args\$args; }
    location /recovery/install/ { try_files \$uri /recovery/install/index.php\$is_args\$args; }
    location / { try_files \$uri /index.php\$is_args\$args; }
    location ~ ^/index\.php(/|$) {
        fastcgi_pass 127.0.0.1:9000;
        fastcgi_split_path_info ^(.+\.php)(/.*)$;
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
        fastcgi_param PATH_INFO \$fastcgi_path_info;
        fastcgi_read_timeout 300;
    }
    location ~ \.php$ { return 404; }
}
NGX
rm -f /etc/nginx/http.d/default.conf
nginx -t
rc-update add nginx default
rc-service nginx restart

cat > /etc/init.d/shopware-worker <<'INIT'
#!/sbin/openrc-run
name="shopware-worker"
description="Shopware messenger consumer"
command="/usr/bin/php83"
command_args="/var/www/shopware/bin/console messenger:consume async failed --time-limit=60 --memory-limit=256M"
command_background="true"
directory="/var/www/shopware"
pidfile="/run/shopware-worker.pid"
output_log="/var/log/shopware-worker.log"
error_log="/var/log/shopware-worker.err.log"
depend() { need mariadb php-fpm83; }
INIT
chmod +x /etc/init.d/shopware-worker

cat > /etc/init.d/shopware-scheduler <<'INIT'
#!/sbin/openrc-run
name="shopware-scheduler"
description="Shopware scheduled task runner"
command="/usr/bin/php83"
command_args="/var/www/shopware/bin/console scheduled-task:run --time-limit=60 --memory-limit=256M"
command_background="true"
directory="/var/www/shopware"
pidfile="/run/shopware-scheduler.pid"
output_log="/var/log/shopware-scheduler.log"
error_log="/var/log/shopware-scheduler.err.log"
depend() { need mariadb php-fpm83; }
INIT
chmod +x /etc/init.d/shopware-scheduler
rc-update add shopware-worker default
rc-update add shopware-scheduler default
rc-service shopware-worker start || true
rc-service shopware-scheduler start || true

cat > /etc/init.d/shopware-firstboot <<'INIT'
#!/sbin/openrc-run
name="shopware-firstboot"
description="First-boot secret rotation for Shopware clones"
command="/usr/local/bin/shopware-firstboot"
command_background="false"
depend() { need mariadb; before php-fpm83 nginx; }
INIT
chmod +x /etc/init.d/shopware-firstboot

cat > /usr/local/bin/shopware-firstboot <<'PY'
#!/usr/bin/env python3
import os, re, subprocess, pathlib
env_path = pathlib.Path('/var/www/shopware/.env')
if env_path.exists():
    text = env_path.read_text()
    m = re.search(r'^APP_SECRET=(.*)$', text, re.M)
    old = (m.group(1).strip() if m else '')
    if old in ('', '__GENERATE__'):
        val = subprocess.check_output(['openssl','rand','-hex','32'], text=True).strip()
        if m:
            text = re.sub(r'^APP_SECRET=.*$', 'APP_SECRET=' + val, text, flags=re.M)
        else:
            text += '\nAPP_SECRET=' + val + '\n'
        env_path.write_text(text)
creds = pathlib.Path('/root/shopware-credentials.env')
if creds.exists():
    text = creds.read_text()
    m = re.search(r'^SHOPWARE_ADMIN_PASSWORD=(.*)$', text, re.M)
    old = (m.group(1).strip() if m else '')
    if old in ('', '__GENERATE__'):
        val = subprocess.check_output(['openssl','rand','-base64','24'], text=True).strip().replace('\n','')
        if m:
            text = re.sub(r'^SHOPWARE_ADMIN_PASSWORD=.*$', 'SHOPWARE_ADMIN_PASSWORD=' + val, text, flags=re.M)
        else:
            text += '\nSHOPWARE_ADMIN_PASSWORD=' + val + '\n'
        creds.write_text(text)
        os.chmod(creds, 0o600)
        subprocess.run(['/usr/bin/php83','/var/www/shopware/bin/console','user:change-password','admin','--password=' + val], cwd='/var/www/shopware', check=False)
PY
chmod +x /usr/local/bin/shopware-firstboot

for i in $(seq 1 30); do
    if curl -fsS "${SHOPWARE_URL}/" >/dev/null 2>&1 || curl -fsS "http://127.0.0.1/" >/dev/null 2>&1; then
        echo "shopware_ready"
        exit 0
    fi
    sleep 2
done
echo "shopware verification failed" >&2
exit 1
BUILD
chmod +x "${BUILD_DIR}/build.sh"

log_info "Building Shopware inside LXC ${CTID}"
ct_sync_dir "${BUILD_DIR}" /tmp/shopware-build
ct_exec "bash /tmp/shopware-build/build.sh"
ct_exec "rm -rf /tmp/shopware-build"

CT_IP="$(get_ct_ip)"
ct_exec "curl -fsS http://127.0.0.1/ >/dev/null"

JSON="$(jq -n \
    --arg node "${TARGET_NODE}" \
    --arg ctid "${CTID}" \
    --arg ip "${CT_IP}" \
    --arg hostname "${HOSTNAME}" \
    --arg url "${SHOPWARE_URL}" \
    --arg admin_user "${ADMIN_USER}" \
    --arg health "http://${CT_IP}/" \
    --arg admin "http://${CT_IP}/admin" \
    '{ok:true,component:"shopware-server",node:$node,ctid:$ctid,ip:$ip,hostname:$hostname,url:$url,admin_user:$admin_user,admin_url:$admin,health:$health,service:"nginx+php-fpm83+mariadb",init:"openrc",arch:"amd64",storage:"local"}')"
write_json_output "${JSON_OUT}" "${JSON}"
log_ok "Shopware LXC ready: ${SHOPWARE_URL} (CTID ${CTID})"
