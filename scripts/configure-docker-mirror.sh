#!/usr/bin/env bash
set -Eeuo pipefail

MIRROR="https://mirror.gcr.io"
CONFIG="/etc/docker/daemon.json"

log() { echo "[dpm] $*"; }

if docker info --format '{{json .RegistryConfig.Mirrors}}' 2>/dev/null \
    | grep -Fq "$MIRROR"; then
  log "Docker registry mirror is already configured"
  exit 0
fi

if [[ -n "$(docker ps -q 2>/dev/null)" ]]; then
  log "Skipping Docker registry mirror configuration: containers are running"
  log "The proxy does not depend on Docker Hub; project pulls may still use Docker Hub fallback"
  exit 0
fi

install -d -m 0755 /etc/docker
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

python3 - "$CONFIG" "$TMP" "$MIRROR" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
mirror = sys.argv[3]

if source.exists() and source.read_text(encoding="utf-8").strip():
    try:
        config = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid {source}: {exc}") from exc
else:
    config = {}

mirrors = config.get("registry-mirrors") or []
if not isinstance(mirrors, list):
    raise SystemExit("registry-mirrors in daemon.json must be a JSON array")
if mirror not in mirrors:
    mirrors.insert(0, mirror)
config["registry-mirrors"] = mirrors
target.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

if command -v dockerd >/dev/null 2>&1; then
  dockerd --validate --config-file "$TMP" >/dev/null
fi

install -o root -g root -m 0644 "$TMP" "$CONFIG"
log "Restarting idle Docker daemon to enable $MIRROR"
systemctl restart docker

docker info --format '{{json .RegistryConfig.Mirrors}}' \
  | grep -Fq "$MIRROR" \
  || { echo "ERROR: Docker registry mirror was not enabled" >&2; exit 1; }

log "Docker Hub cache mirror enabled: $MIRROR"
