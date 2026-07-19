#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "[dpm] install.sh must be run as root" >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/dimka-project-manager"
DATA_DIR="/var/lib/dpm"
LOG_DIR="/var/log/dpm"
RUN_DIR="/run/dpm"
VENV_DIR="$APP_DIR/venv"

log() { echo "[dpm] $*"; }

system_dependencies_ready() {
  command -v python3 >/dev/null 2>&1 \
    && command -v git >/dev/null 2>&1 \
    && command -v ssh-keygen >/dev/null 2>&1 \
    && python3 -c 'import venv, ensurepip' >/dev/null 2>&1
}

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

if ! system_dependencies_ready; then
  install_system_dependencies
fi

# Stop an older installation before changing ownership or its systemd identity.
systemctl stop dimka-project-manager.service 2>/dev/null || true

# Early builds created a dpm account and launched every managed process through
# it. Remove those processes and the account before the root daemon starts.
if id dpm >/dev/null 2>&1; then
  pkill -TERM -u dpm 2>/dev/null || true
  sleep 1
  pkill -KILL -u dpm 2>/dev/null || true
  userdel dpm 2>/dev/null || true
fi

mkdir -p "$APP_DIR" "$DATA_DIR/projects" "$LOG_DIR" "$RUN_DIR" "$DATA_DIR/.ssh"
log "Copying application files"
tar -C "$SOURCE_DIR" \
  --exclude='.git' --exclude='venv' --exclude='__pycache__' --exclude='*.pyc' \
  -cf - . | tar -C "$APP_DIR" -xf -

ensure_virtual_environment
log "Installing Python dependencies"
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --upgrade pip >/dev/null
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements.txt"

# DPM deliberately runs as root. Its Git operations, builds and every managed
# service inherit root privileges. The isolated HOME only keeps DPM's SSH files
# separate from the interactive root account.
if [[ ! -f "$DATA_DIR/.ssh/id_ed25519" ]]; then
  log "Generating Git SSH key"
  ssh-keygen -q -t ed25519 -N '' -C "dpm-root@$(hostname)" -f "$DATA_DIR/.ssh/id_ed25519"
fi
chmod 700 "$DATA_DIR/.ssh"
chmod 600 "$DATA_DIR/.ssh/id_ed25519"
chmod 644 "$DATA_DIR/.ssh/id_ed25519.pub"

chown -R root:root "$APP_DIR" "$DATA_DIR" "$LOG_DIR" "$RUN_DIR"
chmod +x "$APP_DIR/install.sh" "$APP_DIR/update.sh" "$APP_DIR/uninstall.sh" "$APP_DIR/config.sh"

write_systemd_unit
systemctl daemon-reload

cat > /usr/local/bin/dpm <<'EOF_CLI'
#!/usr/bin/env bash
set -e
export DPM_CONFIG_FILE="${DPM_CONFIG_FILE:-/etc/dpm/config.env}"
exec /opt/dimka-project-manager/venv/bin/python -m dpm.cli "$@"
EOF_CLI
chmod +x /usr/local/bin/dpm

log "Configuring administrator and public URL"
DPM_APP_DIR="$APP_DIR" "$APP_DIR/config.sh"

chown -R root:root "$DATA_DIR" "$LOG_DIR" "$RUN_DIR"
systemctl enable --now dimka-project-manager.service

log "Installation complete"
echo
echo "Git SSH public key (add it to GitHub for private repositories):"
echo "----------------------------------------------------------------"
cat "$DATA_DIR/.ssh/id_ed25519.pub"
echo "----------------------------------------------------------------"
echo "Status: dpm status"
echo "Logs:   journalctl -u dimka-project-manager -f"
