#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "[dpm] update.sh must be run as root" >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/dimka-project-manager"
VENV_DIR="$APP_DIR/venv"
CONFIG_FILE="${DPM_CONFIG_FILE:-/etc/dpm/config.env}"

log() { echo "[dpm] $*"; }

install_system_dependencies() {
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "[dpm] Install Python 3 with venv/pip, Git and OpenSSH first" >&2
    exit 1
  fi

  log "Installing system dependencies"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 python3-venv python3-pip git openssh-client
}

ensure_virtual_environment() {
  if [[ -x "$VENV_DIR/bin/python" ]] \
    && "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1; then
    return
  fi

  if [[ -d "$VENV_DIR" ]]; then
    log "Removing incomplete Python virtual environment"
    rm -rf "$VENV_DIR"
  fi

  log "Creating Python virtual environment"
  if ! python3 -m venv "$VENV_DIR"; then
    install_system_dependencies
    rm -rf "$VENV_DIR"
    python3 -m venv "$VENV_DIR"
  fi

  if ! "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1; then
    echo "[dpm] Virtual environment was created without pip" >&2
    exit 1
  fi
}

write_systemd_unit() {
  cat > /etc/systemd/system/dimka-project-manager.service <<'EOF_UNIT'
[Unit]
Description=Dimka Project Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/dimka-project-manager
EnvironmentFile=/etc/dpm/config.env
Environment=HOME=/var/lib/dpm
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/dimka-project-manager/venv/bin/python -m dpm.app
Restart=always
RestartSec=3
KillMode=process
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
EOF_UNIT
}

if ! command -v python3 >/dev/null 2>&1 \
  || ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
  install_system_dependencies
fi

log "Stopping current manager"
systemctl stop dimka-project-manager.service 2>/dev/null || true

# Early builds ran the manager and all child services under a generated dpm
# account. Stop those processes before switching the complete stack to root.
if id dpm >/dev/null 2>&1; then
  pkill -TERM -u dpm 2>/dev/null || true
  sleep 1
  pkill -KILL -u dpm 2>/dev/null || true
fi

log "Updating manager code"
mkdir -p "$APP_DIR"
tar -C "$SOURCE_DIR" \
  --exclude='.git' --exclude='venv' --exclude='__pycache__' --exclude='*.pyc' \
  -cf - . | tar -C "$APP_DIR" -xf -

ensure_virtual_environment
log "Installing Python dependencies"
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --upgrade pip >/dev/null
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements.txt"

if [[ -f "$CONFIG_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi
DATA_DIR="${DPM_DATA_DIR:-/var/lib/dpm}"
LOG_DIR="${DPM_LOG_DIR:-/var/log/dpm}"
RUN_DIR="/run/dpm"
mkdir -p "$DATA_DIR/projects" "$DATA_DIR/.ssh" "$LOG_DIR" "$RUN_DIR"

chown -R root:root "$APP_DIR" "$DATA_DIR" "$LOG_DIR" "$RUN_DIR"
chmod +x "$APP_DIR/install.sh" "$APP_DIR/update.sh" "$APP_DIR/uninstall.sh" "$APP_DIR/config.sh"
[[ ! -f "$CONFIG_FILE" ]] || { chown root:root "$CONFIG_FILE"; chmod 600 "$CONFIG_FILE"; }

write_systemd_unit
systemctl daemon-reload

# The obsolete account is no longer referenced by systemd or any runtime file.
if id dpm >/dev/null 2>&1; then
  userdel dpm 2>/dev/null || true
fi

systemctl enable --now dimka-project-manager.service

echo "[dpm] Update complete: manager and managed services now run as root"
