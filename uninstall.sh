#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "[dpm] uninstall.sh must be run as root" >&2
  exit 1
fi

PURGE=0
if [[ "${1:-}" == "--purge" ]]; then PURGE=1; fi

systemctl disable --now dimka-project-manager.service 2>/dev/null || true
# Stop any managed processes that deliberately survived the manager shutdown.
pkill -TERM -u dpm 2>/dev/null || true
sleep 1
pkill -KILL -u dpm 2>/dev/null || true

rm -f /etc/systemd/system/dimka-project-manager.service /usr/local/bin/dpm
systemctl daemon-reload
rm -rf /opt/dimka-project-manager /run/dpm

if [[ $PURGE -eq 1 ]]; then
  rm -rf /var/lib/dpm /var/log/dpm /etc/dpm
  userdel dpm 2>/dev/null || true
  echo "[dpm] Removed manager, projects, logs and configuration"
else
  echo "[dpm] Removed manager. Data and configuration were preserved."
  echo "[dpm] Run './uninstall.sh --purge' to remove everything."
fi
