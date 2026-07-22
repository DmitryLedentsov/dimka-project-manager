#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${DPM_APP_DIR:-/opt/deploy-project-manager}"
COMPOSE_FILE="$APP_DIR/infra/compose.yml"

log() { echo "[dpm] $*"; }

docker network inspect dpm-proxy >/dev/null 2>&1 \
  || docker network create dpm-proxy >/dev/null

bash "$APP_DIR/scripts/build-proxy-image.sh"

log "Starting DPM proxy from the local verified image"
docker compose \
  -p dpm-infra \
  -f "$COMPOSE_FILE" \
  up -d --pull never
