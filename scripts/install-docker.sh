#!/usr/bin/env bash
set -Eeuo pipefail

if docker version >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  systemctl enable --now docker >/dev/null 2>&1 || true
  exit 0
fi

. /etc/os-release
case "${ID:-}" in
  ubuntu|debian) ;;
  *) echo "DPM automatic Docker installation supports Ubuntu/Debian only" >&2; exit 1 ;;
esac

apt-get update
apt-get install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
ARCH="$(dpkg --print-architecture)"
CODENAME="${UBUNTU_CODENAME:-${VERSION_CODENAME}}"
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/${ID}
Suites: ${CODENAME}
Components: stable
Architectures: ${ARCH}
Signed-By: /etc/apt/keyrings/docker.asc
EOF

for package in docker.io docker-compose docker-compose-v2 docker-doc podman-docker containerd runc; do
  dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed' && apt-get remove -y "$package" || true
done
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker version >/dev/null
docker compose version >/dev/null
