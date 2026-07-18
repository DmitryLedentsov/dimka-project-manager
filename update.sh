#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "[dpm] update.sh must be run as root" >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/dimka-project-manager"

echo "[dpm] Updating manager code"
systemctl stop dimka-project-manager.service || true
mkdir -p "$APP_DIR"
tar -C "$SOURCE_DIR" \
  --exclude='.git' --exclude='venv' --exclude='__pycache__' --exclude='*.pyc' \
  -cf - . | tar -C "$APP_DIR" -xf -

if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --disable-pip-version-check -r "$APP_DIR/requirements.txt"
chown -R root:root "$APP_DIR"
chmod +x "$APP_DIR/install.sh" "$APP_DIR/update.sh" "$APP_DIR/uninstall.sh" "$APP_DIR/config.sh"
systemctl daemon-reload
systemctl start dimka-project-manager.service

echo "[dpm] Update complete"
