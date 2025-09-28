#!/usr/bin/env bash
set -euo pipefail

# bootstrap_vps.sh
# Run once per VPS to prepare environment.
# Supports Debian/Ubuntu. Adapt package manager for other distros.

APP_REPO="https://github.com/Idem-AI/vps-deployment.git"
NGINX_DIR="/opt/vps-deployment/nginx-certbot"
APP_BASE="/opt"
NETWORK_NAME="proxy_net"

echo "=== Bootstrap VPS: installation docker + nginx-certbot ==="

# 1) update & install dependencies
echo "[1/5] Mise à jour et installation des paquets requis..."
sudo apt-get update -y
sudo apt-get install -y apt-transport-https ca-certificates curl gnupg lsb-release git

# 2) Install Docker (official)
if ! command -v docker >/dev/null 2>&1; then
  echo "[2/5] Installation de Docker..."
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu \
    $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io
fi

# 3) Install docker-compose (standalone) if absent
if ! command -v docker-compose >/dev/null 2>&1; then
  echo "[3/5] Installation docker-compose (v2 standalone)..."
  DOCKER_COMPOSE_BIN="/usr/local/bin/docker-compose"
  sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o "$DOCKER_COMPOSE_BIN"
  sudo chmod +x "$DOCKER_COMPOSE_BIN"
fi

echo "deploiement de l'outil yq..."
# Vérifier si yq est installé
if ! command -v yq >/dev/null 2>&1; then
  echo "[!] yq n'est pas installé, installation en cours..."

  ARCH=$(uname -m)
  case $ARCH in
    x86_64) ARCH="amd64" ;;
    aarch64 | arm64) ARCH="arm64" ;;
    *) echo "Architecture non supportée automatiquement : $ARCH"; exit 1 ;;
  esac

  wget -q "https://github.com/mikefarah/yq/releases/latest/download/yq_linux_${ARCH}" -O /usr/local/bin/yq
  chmod +x /usr/local/bin/yq

  echo "[✓] yq installé avec succès : $(yq --version)"
else
  echo "[✓] yq déjà installé : $(yq --version)"
fi

# 4) Clone nginx-certbot repo if absent
echo "[4/5] Clone du repo $APP_REPO dans $APP_BASE (si absent)..."

  cd "$APP_BASE" || exit 1
  git clone "$APP_REPO" 

# 5) create network and start nginx stack
echo "[5/5] Création réseau $NETWORK_NAME et démarrage du stack nginx-certbot..."
docker network inspect "$NETWORK_NAME" >/dev/null 2>&1 || docker network create "$NETWORK_NAME"

cd "$NGINX_DIR"
# Start nginx (service name used by repo is 'nginx'); start full stack if needed
docker-compose up -d nginx || docker-compose up -d || true

echo " Bootstrap terminé..."
