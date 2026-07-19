#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "[dpm] uninstall.sh must be run as root" >&2
  exit 1
fi

PURGE=0
if [[ "${1:-}" == "--purge" ]]; then PURGE=1; fi

CONFIG_FILE="${DPM_CONFIG_FILE:-/etc/dpm/config.env}"
if [[ -f "$CONFIG_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi
DATA_DIR="${DPM_DATA_DIR:-/var/lib/dpm}"
DB_PATH="$DATA_DIR/dpm.sqlite3"

systemctl disable --now dimka-project-manager.service 2>/dev/null || true

# Managed services run as root, so never use pkill -u root. Stop only the exact
# process groups recorded by DPM in SQLite.
if [[ -f "$DB_PATH" ]]; then
  python3 - "$DB_PATH" <<'PY'
import os
import signal
import sqlite3
import sys
import time

path = sys.argv[1]
try:
    connection = sqlite3.connect(path)
    rows = connection.execute("SELECT pid FROM services WHERE pid IS NOT NULL").fetchall()
finally:
    try:
        connection.close()
    except Exception:
        pass

pids = [int(row[0]) for row in rows if row and row[0]]
for pid in pids:
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass

time.sleep(1)
for pid in pids:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
PY
fi

rm -f /etc/systemd/system/dimka-project-manager.service /usr/local/bin/dpm
systemctl daemon-reload
rm -rf /opt/dimka-project-manager /run/dpm

# Clean up the obsolete account created only by pre-root DPM versions.
if id dpm >/dev/null 2>&1; then
  pkill -TERM -u dpm 2>/dev/null || true
  userdel dpm 2>/dev/null || true
fi

if [[ $PURGE -eq 1 ]]; then
  rm -rf /var/lib/dpm /var/log/dpm /etc/dpm
  echo "[dpm] Removed manager, projects, logs and configuration"
else
  echo "[dpm] Removed manager. Data and configuration were preserved."
  echo "[dpm] Run './uninstall.sh --purge' to remove everything."
fi
