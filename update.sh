#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "[dpm] update.sh must be run as root" >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/dimka-project-manager"
VENV_DIR="$APP_DIR/venv"

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

if ! command -v python3 >/dev/null 2>&1 \
  || ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
  install_system_dependencies
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

chown -R root:root "$APP_DIR"
chmod +x "$APP_DIR/install.sh" "$APP_DIR/update.sh" "$APP_DIR/uninstall.sh" "$APP_DIR/config.sh"
systemctl daemon-reload
systemctl restart dimka-project-manager.service

echo "[dpm] Update complete"
