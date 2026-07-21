#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${DPM_APP_DIR:-/opt/deploy-project-manager}"
COMPOSE_FILE="$APP_DIR/infra/compose.yml"
TRAEFIK_IMAGE="traefik:v3.6"
DELAYS=(5 15 30 60 120 240)

log() { echo "[dpm] $*"; }

docker network inspect dpm-proxy >/dev/null 2>&1 \
  || docker network create dpm-proxy >/dev/null

if ! docker image inspect "$TRAEFIK_IMAGE" >/dev/null 2>&1; then
  for attempt in $(seq 1 ${#DELAYS[@]}); do
    log "Pulling $TRAEFIK_IMAGE (attempt $attempt/${#DELAYS[@]})"
    if docker pull "$TRAEFIK_IMAGE"; then
      break
    fi
    if [[ "$attempt" -eq "${#DELAYS[@]}" ]]; then
      echo "ERROR: Docker Hub still refuses $TRAEFIK_IMAGE after retries." >&2
      echo "Authenticate with 'docker login' or wait for the Docker Hub limit to reset, then rerun update.sh." >&2
      exit 1
    fi
    delay="${DELAYS[$((attempt - 1))]}"
    log "Registry throttled the request; retrying in ${delay}s"
    sleep "$delay"
  done
fi

docker compose -p dpm-infra -f "$COMPOSE_FILE" up -d --pull never
