#!/usr/bin/env bash
set -Eeuo pipefail
CONFIG_FILE="${DPM_CONFIG_FILE:-/etc/dpm/config.env}"
[[ -f "$CONFIG_FILE" ]] || { echo "Missing $CONFIG_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
source "$CONFIG_FILE"
BASE_PATH="${DPM_BASE_PATH:-/admin}"
BASE_PATH="/${BASE_PATH#/}"
BASE_PATH="${BASE_PATH%/}"
[[ -n "$BASE_PATH" ]] || BASE_PATH="/admin"
PORT="${DPM_PORT:-8787}"
cat > /etc/dpm/traefik-dynamic.yml <<EOF
http:
  routers:
    dpm-control-plane:
      rule: "PathPrefix(\`${BASE_PATH}\`)"
      entryPoints: [web]
      priority: 10000
      service: dpm-control-plane
  services:
    dpm-control-plane:
      loadBalancer:
        servers:
          - url: "http://127.0.0.1:${PORT}"
EOF
chmod 600 /etc/dpm/traefik-dynamic.yml
chown root:root /etc/dpm/traefik-dynamic.yml
