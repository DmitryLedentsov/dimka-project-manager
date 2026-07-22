#!/usr/bin/env bash
set -Eeuo pipefail

TRAEFIK_VERSION="${DPM_TRAEFIK_VERSION:-3.7.1}"
IMAGE="dpm-traefik:${TRAEFIK_VERSION}"
RELEASE_BASE="https://github.com/traefik/traefik/releases/download/v${TRAEFIK_VERSION}"

log() { echo "[dpm] $*"; }

if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  log "Using existing local proxy image $IMAGE"
  exit 0
fi

case "$(uname -m)" in
  x86_64|amd64)
    ARCH="amd64"
    SHA256="e92bcfb03fa1e6a70c4e7ad4eb4f1604967e6fa3c21d8e7605aca5407a40162c"
    ;;
  aarch64|arm64)
    ARCH="arm64"
    SHA256="911ad9f4c21a58fdcbf09c75d967a280c9eec22b3d056fc7f4950cd3294c22b8"
    ;;
  *)
    echo "ERROR: Unsupported architecture for Traefik: $(uname -m)" >&2
    exit 1
    ;;
esac

ASSET="traefik_v${TRAEFIK_VERSION}_linux_${ARCH}.tar.gz"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

log "Downloading official Traefik v${TRAEFIK_VERSION} release from GitHub"
curl --fail --location --silent --show-error \
  --retry 5 --retry-delay 3 --retry-all-errors \
  --connect-timeout 15 --max-time 600 \
  "${RELEASE_BASE}/${ASSET}" \
  --output "$WORK_DIR/$ASSET"

echo "${SHA256}  $WORK_DIR/$ASSET" | sha256sum --check --status \
  || { echo "ERROR: Traefik release checksum verification failed" >&2; exit 1; }

tar -xzf "$WORK_DIR/$ASSET" -C "$WORK_DIR" traefik
chmod 0755 "$WORK_DIR/traefik"

if [[ -f /etc/ssl/certs/ca-certificates.crt ]]; then
  cp /etc/ssl/certs/ca-certificates.crt "$WORK_DIR/ca-certificates.crt"
else
  : > "$WORK_DIR/ca-certificates.crt"
fi

cat > "$WORK_DIR/Dockerfile" <<'EOF'
FROM scratch
COPY traefik /traefik
COPY ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
ENTRYPOINT ["/traefik"]
EOF

log "Building local proxy image $IMAGE without registry access"
docker build \
  --pull=false \
  --network=none \
  --tag "$IMAGE" \
  "$WORK_DIR"

docker image inspect "$IMAGE" >/dev/null
log "Local proxy image is ready: $IMAGE"
