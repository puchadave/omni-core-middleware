#!/bin/bash

# setup_alpine_lxc.sh
# Fully automated bootstrap installation script for a fresh Alpine Linux LXC container
# on Proxmox VE. This script updates packages, installs Python, Git, SQLite, sets up a
# virtual environment, installs application dependencies, and configures a systemd
# service for automatic startup of the Omni-Core Middleware.

# --- Configuration Variables ---
APP_DIR="/opt/omni-middleware"
PYTHON_VERSION="3.11"
VENV_DIR=".venv"
USER="omniuser"
GROUP="omniuser"
SERVICE_NAME="omni-middleware"
GIT_REPO="https://github.com/puchadave/omni-core-middleware.git"

# --- Logging and Error Handling ---
LOG_FILE="/var/log/${SERVICE_NAME}_setup.log"
exec > >(tee -a "$LOG_FILE") 2>&1

set -euo pipefail

log_info() {
    echo "$(date +'%Y-%m-%d %H:%M:%S') INFO: $1"
}

log_error() {
    echo "$(date +'%Y-%m-%d %H:%M:%S') ERROR: $1" >&2
    exit 1
}

check_root() {
    if [ "$(id -u)" -ne 0 ]; then
        log_error "This script must be run as root."
    fi
}

# --- Main Setup Steps ---
check_root

log_info "Starting Omni-Core Middleware LXC setup..."

# 1. Update system and install necessary packages
log_info "Updating system packages..."
apk update || log_error "Failed to update apk."
apk add --no-cache \
    python${PYTHON_VERSION} \
    python${PYTHON_VERSION}-dev \
    py${PYTHON_VERSION}-pip \
    py${PYTHON_VERSION}-virtualenv \
    git \
    sqlite \
    curl \
    build-base \
    libffi-dev \
    || log_error "Failed to install required packages."

# Ensure python and pip point to the desired version
ln -sf /usr/bin/python${PYTHON_VERSION} /usr/local/bin/python
ln -sf /usr/bin/pip${PYTHON_VERSION} /usr/local/bin/pip

# 2. Create dedicated user and group
log_info "Creating dedicated user and group '${USER}'..."
if ! id -g "${GROUP}" >/dev/null 2>&1; then
    addgroup -S "${GROUP}" || log_error "Failed to create group ${GROUP}."
fi
if ! id -u "${USER}" >/dev/null 2>&1; then
    adduser -S -G "${GROUP}" "${USER}" || log_error "Failed to create user ${USER}."
fi

# 3. Clone the Git repository
log_info "Cloning Git repository into ${APP_DIR}..."
if [ -d "${APP_DIR}" ]; then
    log_info "${APP_DIR} already exists, attempting to pull latest changes."
    chown -R "${USER}":"${GROUP}" "${APP_DIR}"
    su -l "${USER}" -c "cd ${APP_DIR} && git pull" || log_error "Failed to pull latest changes."
else
    git clone "${GIT_REPO}" "${APP_DIR}" || log_error "Failed to clone repository."
    chown -R "${USER}":"${GROUP}" "${APP_DIR}" || log_error "Failed to change ownership of ${APP_DIR}."
fi

# 4. Set up Python virtual environment and install dependencies
log_info "Setting up Python virtual environment and installing dependencies..."
su -l "${USER}" -c "cd ${APP_DIR} && python -m venv ${VENV_DIR} && source ${VENV_DIR}/bin/activate && pip install --upgrade pip && pip install -r ${APP_DIR}/requirements.txt" || log_error "Failed to set up virtual environment or install dependencies."

# 5. Configure systemd service for auto-start
log_info "Configuring systemd service '${SERVICE_NAME}.service'..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

cat <<EOF >"${SERVICE_FILE}"
[Unit]
Description=Omni-Core Middleware FastAPI Application
After=network.target

[Service]
User=${USER}
Group=${GROUP}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1 --loop uvloop --interface auto
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "${SERVICE_FILE}" || log_error "Failed to set permissions on service file."
systemctl daemon-reload || log_error "Failed to reload systemd daemon."
systemctl enable "${SERVICE_NAME}.service" || log_error "Failed to enable ${SERVICE_NAME} service."
systemctl start "${SERVICE_NAME}.service" || log_error "Failed to start ${SERVICE_NAME} service."

log_info "Omni-Core Middleware LXC setup complete. Service '${SERVICE_NAME}.service' started."
log_info "Check service status with: systemctl status ${SERVICE_NAME}.service"
log_info "View logs with: journalctl -u ${SERVICE_NAME}.service -f"