#!/usr/bin/env bash
set -euo pipefail

# bootstrap_orchestrator.sh
# Bootstraps and installs the orchestrator FastAPI app as a systemd service
#
# Usage:
#   sudo bash bootstrap_orchestrator.sh [--no-bootstrap] [OPTIONS]
#
# Options (environment-like CLI overrides):
#   APP_DIR=/opt/vps-deployment            location where orchestrator_minimal.py will live (default /opt/vps-deployment)
#   APP_SRC=""                             optional path to copy orchestrator_minimal.py from
#   VENV_DIR=/opt/vps-deployment/venv      virtualenv path
#   STORAGE_DIR=/var/lib/orch_b            storage for agents/jobs/envs
#   SERVICE_NAME=orchestrator.service      systemd unit name
#   ORCH_USER=orch                         system user to run the service
#   ADMIN_API_TOKEN=""                     optional admin token (if empty, no admin auth enforced)
#
# Example:
#   sudo APP_SRC=./orchestrator_minimal.py ADMIN_API_TOKEN=secret bash bootstrap_orchestrator.sh
#

# --- config (override by setting env vars before running script or passing via CLI) ---

NO_BOOTSTRAP_FLAG="${1:-}"

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SRC="${APP_SRC:-orchestrator.py}"   # if provided, will copy to $APP_DIR/orchestrator_minimal.py
APP_MODULE="${APP_MODULE:-orchestrator:app}"  # uvicorn import path (module:app)
VENV_DIR="${VENV_DIR:-$APP_DIR/venv}"
STORAGE_DIR="${STORAGE_DIR:-/var/lib/orch_b}"
SERVICE_NAME="${SERVICE_NAME:-orchestrator.service}"
ORCH_USER="${ORCH_USER:-root}"
ADMIN_API_TOKEN="${ADMIN_API_TOKEN:-}"
PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"

REQS="fastapi uvicorn[standard] python-multipart pydantic"


BOOTSTRAP_PATH="$APP_DIR/bootstrap.sh"

# ---- helper funcs ----
info(){ echo -e "\033[1;34m[INFO]\033[0m $*"; }
warn(){ echo -e "\033[1;33m[WARN]\033[0m $*"; }
err(){ echo -e "\033[1;31m[ERROR]\033[0m $*"; }

if [ "$(id -u)" -ne 0 ]; then
  err "This script must be run as root (sudo)."
  exit 2
fi


info "Bootstrap orchestrator: APP_DIR=$APP_DIR, VENV_DIR=$VENV_DIR, STORAGE_DIR=$STORAGE_DIR, SERVICE=$SERVICE_NAME"

# 1) system deps
info "Installing system packages (python3-venv, python3-pip, git, curl)..."
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git curl


# 3) create app dir and copy app if provided
mkdir -p "$APP_DIR"
chmod 750 "$APP_DIR"

if [ -n "$APP_SRC" ]; then
  if [ ! -f "$APP_SRC" ]; then
    err "Provided APP_SRC does not exist: $APP_SRC"
    exit 3
  fi

  chown "$ORCH_USER:$ORCH_USER" "$APP_DIR/orchestrator.py"
fi

if [ ! -f "$APP_DIR/orchestrator.py" ]; then
  warn "No orchestrator.py found in $APP_DIR. Please place your app at $APP_DIR/orchestrator.py and re-run, or pass APP_SRC to copy it now."
  # do not exit to allow manual placement later, but service will fail until file exists
fi

# 4) create venv and install python deps as orch user
if [ ! -d "$VENV_DIR" ]; then
  info "Creating virtualenv in $VENV_DIR (user: $ORCH_USER)"
  sudo -u "$ORCH_USER" python3 -m venv "$VENV_DIR"
fi

info "Installing Python dependencies into venv"
sudo -u "$ORCH_USER" "$VENV_DIR/bin/pip" install --upgrade pip
sudo -u "$ORCH_USER" "$VENV_DIR/bin/pip" install $REQS

# 5) create storage dir and init JSON files
info "Preparing storage dir $STORAGE_DIR"
mkdir -p "$STORAGE_DIR/envs"
chown -R "$ORCH_USER:$ORCH_USER" "$STORAGE_DIR"
chmod 750 "$STORAGE_DIR"

AGENTS_FILE="$STORAGE_DIR/agents.json"
JOBS_FILE="$STORAGE_DIR/jobs.json"

if [ ! -f "$AGENTS_FILE" ]; then
  echo "{}" > "$AGENTS_FILE"
  chown "$ORCH_USER:$ORCH_USER" "$AGENTS_FILE"
  chmod 600 "$AGENTS_FILE"
fi
if [ ! -f "$JOBS_FILE" ]; then
  echo "{}" > "$JOBS_FILE"
  chown "$ORCH_USER:$ORCH_USER" "$JOBS_FILE"
  chmod 600 "$JOBS_FILE"
fi

# 6) create systemd service unit
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"
info "Writing systemd unit to $SERVICE_PATH"

# prepare environment lines
ENV_LINES="Environment=PYTHONUNBUFFERED=1"
ENV_LINES="$ENV_LINES
Environment=APPS_BASE=$APP_DIR/apps
Environment=ORCH_B_STORAGE=$STORAGE_DIR
Environment=VENV_DIR=$VENV_DIR
Environment=PORT=$PORT
Environment=HOST=$HOST"

if [ -n "$ADMIN_API_TOKEN" ]; then
  ENV_LINES="$ENV_LINES
Environment=ADMIN_API_TOKEN=$ADMIN_API_TOKEN"
fi

cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=Orchestrator API (FastAPI) - orchestrator
After=network.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=$APP_DIR
$ENV_LINES
ExecStart=$VENV_DIR/bin/uvicorn $APP_MODULE --host $HOST --port $PORT --workers 1
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# set permissions
chmod 644 "$SERVICE_PATH"

# 7) reload systemd and start service
info "Reloading systemd and starting $SERVICE_NAME"
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

# 8) wait briefly and show status
sleep 1
systemctl status "$SERVICE_NAME" --no-pager

# 9) optional quick health-check
info "Health check: curl http://127.0.0.1:$PORT/ (may fail until app file available)"
sleep 1
if command -v curl >/dev/null 2>&1; then
  set +e
  curl -sS -m 5 "http://127.0.0.1:$PORT/" || true
  set -e
fi
# Run bootstrap unless --no-bootstrap is passed
if [ "$NO_BOOTSTRAP_FLAG" = "--no-bootstrap" ]; then
  echo "[install-agent] Skipping bootstrap (user requested --no-bootstrap)."
else
  echo "[install-agent] Running bootstrap to prepare VPS (may take several minutes)..."
  bash "$BOOTSTRAP_PATH"
  echo "[install-agent] Bootstrap finished."
fi


info "Bootstrap finished. If the service failed, inspect logs with 'sudo journalctl -u $SERVICE_NAME -f'."
info "Application path: $APP_DIR/orchestrator.py"
info "Storage path: $STORAGE_DIR (agents.json, jobs.json, envs/)"
