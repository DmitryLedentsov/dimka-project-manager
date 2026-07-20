#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "update.sh must be run as root" >&2; exit 1; }
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/deploy-project-manager"
OLD_APP_DIR="/opt/dimka-project-manager"
CONFIG_FILE="${DPM_CONFIG_FILE:-/etc/dpm/config.env}"
NEW_UNIT="deploy-project-manager.service"
OLD_UNIT="dimka-project-manager.service"

log(){ echo "[dpm] $*"; }
log "Stopping old and new manager units"
systemctl stop "$OLD_UNIT" 2>/dev/null || true
systemctl stop "$NEW_UNIT" 2>/dev/null || true

if [[ -f "$CONFIG_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi
DATA_DIR="${DPM_DATA_DIR:-/var/lib/dpm}"
DB_PATH="$DATA_DIR/dpm.sqlite3"
if [[ -f "$DB_PATH" ]]; then
  python3 - "$DB_PATH" <<'PY'
import os, signal, sqlite3, sys
path=sys.argv[1]
try:
    db=sqlite3.connect(path)
    tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "services" in tables:
        for (pid,) in db.execute("SELECT pid FROM services WHERE pid IS NOT NULL"):
            try: os.killpg(int(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, ValueError): pass
except sqlite3.Error:
    pass
PY
fi

bash "$SOURCE_DIR/scripts/install-docker.sh"
apt-get update
apt-get install -y python3 python3-venv python3-pip git openssh-client
install -d -o root -g root -m 700 /etc/dpm "$DATA_DIR" "$DATA_DIR/projects" "$DATA_DIR/.ssh" "${DPM_LOG_DIR:-/var/log/dpm}"

if [[ -f "$CONFIG_FILE" ]]; then
  python3 - "$CONFIG_FILE" <<'PY'
from pathlib import Path
import sys
path=Path(sys.argv[1]); lines=path.read_text().splitlines(); out=[]; seen=set()
for line in lines:
    if '=' not in line or line.lstrip().startswith('#'):
        out.append(line); continue
    key=line.split('=',1)[0].strip(); seen.add(key)
    if key=='DPM_HOST': out.append("DPM_HOST='127.0.0.1'")
    elif key=='DPM_PORT': out.append("DPM_PORT='8787'")
    else: out.append(line)
if 'DPM_HOST' not in seen: out.append("DPM_HOST='127.0.0.1'")
if 'DPM_PORT' not in seen: out.append("DPM_PORT='8787'")
path.write_text('\n'.join(out)+'\n')
PY
else
  echo "Missing $CONFIG_FILE; run ./install.sh instead" >&2
  exit 1
fi
chmod 600 "$CONFIG_FILE"

rm -rf "$APP_DIR"
install -d -o root -g root -m 755 "$APP_DIR"
tar -C "$SOURCE_DIR" --exclude='.git' --exclude='venv' --exclude='__pycache__' --exclude='*.pyc' -cf - . | tar -C "$APP_DIR" -xf -
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip >/dev/null
"$APP_DIR/venv/bin/python" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements.txt"
cat > /usr/local/bin/dpm <<EOF
#!/usr/bin/env bash
exec "$APP_DIR/venv/bin/python" -m dpm.cli "\$@"
EOF
chmod 755 /usr/local/bin/dpm
cat > /etc/systemd/system/$NEW_UNIT <<EOF
[Unit]
Description=Deploy Project Manager
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$CONFIG_FILE
Environment=HOME=$DATA_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/venv/bin/python -m dpm.app
Restart=always
RestartSec=3
KillMode=control-group
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF
systemctl disable "$OLD_UNIT" 2>/dev/null || true
rm -f /etc/systemd/system/$OLD_UNIT
systemctl daemon-reload
bash "$APP_DIR/scripts/configure-proxy.sh"
# Traefik is the only public reverse proxy in the Compose-native architecture.
systemctl disable --now nginx 2>/dev/null || true
rm -f /etc/nginx/sites-enabled/tank-game.conf /etc/nginx/sites-available/tank-game.conf
docker compose -p dpm-infra -f "$APP_DIR/infra/compose.yml" up -d
systemctl enable --now "$NEW_UNIT"
rm -rf "$OLD_APP_DIR"
echo "[dpm] Compose-native update complete"
