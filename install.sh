#!/usr/bin/env bash
set -Eeuo pipefail
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "install.sh must be run as root" >&2; exit 1; }
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/deploy-project-manager"
CONFIG_DIR="/etc/dpm"
CONFIG_FILE="$CONFIG_DIR/config.env"
DATA_DIR="/var/lib/dpm"
LOG_DIR="/var/log/dpm"
UNIT="deploy-project-manager.service"

log(){ echo "[dpm] $*"; }
log "Installing Docker Engine and Compose plugin"
bash "$SOURCE_DIR/scripts/install-docker.sh"
apt-get update
apt-get install -y python3 python3-venv python3-pip git openssh-client

install -d -o root -g root -m 700 "$CONFIG_DIR" "$DATA_DIR" "$DATA_DIR/projects" "$DATA_DIR/.ssh" "$LOG_DIR"
if [[ ! -f "$DATA_DIR/.ssh/id_ed25519" ]]; then
  ssh-keygen -t ed25519 -N '' -f "$DATA_DIR/.ssh/id_ed25519" -C 'dpm@server' >/dev/null
fi
chmod 700 "$DATA_DIR/.ssh"; chmod 600 "$DATA_DIR/.ssh/id_ed25519"; chmod 644 "$DATA_DIR/.ssh/id_ed25519.pub"

if [[ ! -f "$CONFIG_FILE" ]]; then
  SERVER_IP="$(hostname -I | awk '{print $1}')"
  SECRET="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
  TOKEN="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"
  cat > "$CONFIG_FILE" <<EOF
DPM_HOST='127.0.0.1'
DPM_PORT='8787'
DPM_BASE_PATH='/admin'
DPM_PUBLIC_URL='http://${SERVER_IP}/admin'
DPM_DATA_DIR='/var/lib/dpm'
DPM_LOG_DIR='/var/log/dpm'
DPM_POLL_INTERVAL='60'
DPM_COMPOSE_TIMEOUT='240'
DPM_SECRET_KEY='${SECRET}'
DPM_CLI_TOKEN='${TOKEN}'
DPM_ADMIN_USERNAME='admin'
DPM_ADMIN_IS_DEFAULT='1'
EOF
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

cat > /etc/systemd/system/$UNIT <<EOF
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
rm -f /etc/systemd/system/dimka-project-manager.service
systemctl daemon-reload

bash "$APP_DIR/scripts/configure-proxy.sh"
# Compose-native DPM uses Traefik as the only owner of public ports 80/443.
systemctl disable --now nginx 2>/dev/null || true
rm -f /etc/nginx/sites-enabled/tank-game.conf /etc/nginx/sites-available/tank-game.conf
docker compose -p dpm-infra -f "$APP_DIR/infra/compose.yml" up -d
systemctl enable --now "$UNIT"

# shellcheck disable=SC1090
source "$CONFIG_FILE"
echo "[dpm] Installed: $DPM_PUBLIC_URL"
echo "[dpm] Default login: admin / admin"
echo "[dpm] Git public key:"
cat "$DATA_DIR/.ssh/id_ed25519.pub"
