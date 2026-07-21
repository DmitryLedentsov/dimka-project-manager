#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "config.sh must be run as root" >&2; exit 1; }
CONFIG_FILE="${DPM_CONFIG_FILE:-/etc/dpm/config.env}"
APP_DIR="/opt/deploy-project-manager"
[[ -f "$CONFIG_FILE" ]] || { echo "Run install.sh first" >&2; exit 1; }
EDITOR_COMMAND="${EDITOR:-nano}"
"$EDITOR_COMMAND" "$CONFIG_FILE"
chmod 600 "$CONFIG_FILE"
bash "$APP_DIR/scripts/configure-proxy.sh"
systemctl restart deploy-project-manager.service
bash "$APP_DIR/scripts/start-proxy.sh"
echo "[dpm] Configuration updated"
