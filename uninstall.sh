#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "uninstall.sh must be run as root" >&2; exit 1; }
APP_DIR="/opt/deploy-project-manager"
systemctl disable --now deploy-project-manager.service 2>/dev/null || true
if [[ -f "$APP_DIR/infra/compose.yml" ]]; then docker compose -p dpm-infra -f "$APP_DIR/infra/compose.yml" down || true; fi
rm -f /etc/systemd/system/deploy-project-manager.service /usr/local/bin/dpm
systemctl daemon-reload
rm -rf "$APP_DIR"
echo "DPM application removed. /var/lib/dpm, /var/log/dpm, /etc/dpm and Docker volumes were preserved."
