#!/usr/bin/env bash
# ------------------------------------------------------------------
# One-time provisioning for Hetzner CPX41 (Ubuntu 24.04)
# Run as root on a fresh VPS:  bash scripts/setup-vps.sh
# ------------------------------------------------------------------
set -euo pipefail

DEPLOY_USER="deploy"
REPO_URL="${REPO_URL:-git@github.com:tontonaviella/mundi.ai.git}"
REPO_DIR="/home/${DEPLOY_USER}/mundi.ai"
DOMAIN="gis.nozalabs.rw"
S3_DOMAIN="s3.gis.nozalabs.rw"
EMAIL="${CERTBOT_EMAIL:-admin@nozalabs.rw}"

echo "=== [1/9] System update + timezone ==="
apt-get update && apt-get upgrade -y
timedatectl set-timezone UTC

echo "=== [2/9] Create deploy user ==="
if ! id "$DEPLOY_USER" &>/dev/null; then
  adduser --disabled-password --gecos "" "$DEPLOY_USER"
  usermod -aG sudo "$DEPLOY_USER"
  echo "${DEPLOY_USER} ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/${DEPLOY_USER}"
  chmod 440 "/etc/sudoers.d/${DEPLOY_USER}"
  # Copy SSH authorized_keys from root
  mkdir -p "/home/${DEPLOY_USER}/.ssh"
  cp /root/.ssh/authorized_keys "/home/${DEPLOY_USER}/.ssh/"
  chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "/home/${DEPLOY_USER}/.ssh"
  chmod 700 "/home/${DEPLOY_USER}/.ssh"
  chmod 600 "/home/${DEPLOY_USER}/.ssh/authorized_keys"
  echo "Created user: ${DEPLOY_USER}"
else
  echo "User ${DEPLOY_USER} already exists"
fi

echo "=== [3/9] Firewall (UFW) ==="
apt-get install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status

echo "=== [4/9] Install Docker Engine + Compose ==="
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sh
  usermod -aG docker "$DEPLOY_USER"
  systemctl enable docker
  echo "Docker installed"
else
  echo "Docker already installed: $(docker --version)"
fi

echo "=== [5/9] Install certbot ==="
apt-get install -y certbot

echo "=== [6/9] Obtain SSL certificates ==="
# Ensure DNS A records point here BEFORE running this step
if [ ! -d "/etc/letsencrypt/live/${DOMAIN}" ]; then
  echo "Requesting certificate for ${DOMAIN} and ${S3_DOMAIN}..."
  certbot certonly --standalone \
    -d "$DOMAIN" \
    -d "$S3_DOMAIN" \
    --email "$EMAIL" \
    --agree-tos \
    --non-interactive
else
  echo "Certificate already exists for ${DOMAIN}"
fi

echo "=== [7/9] Clone repository ==="
if [ ! -d "$REPO_DIR" ]; then
  sudo -u "$DEPLOY_USER" git clone "$REPO_URL" "$REPO_DIR"
  echo "Cloned to ${REPO_DIR}"
else
  echo "Repository already exists at ${REPO_DIR}"
fi

echo "=== [8/9] Create data directories ==="
# Data lives under the repo dir (mapped by docker-compose volumes: ./data/*)
mkdir -p "${REPO_DIR}/data/postgres"
mkdir -p "${REPO_DIR}/data/minio"
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${REPO_DIR}/data"

# Certbot webroot for renewal challenges
mkdir -p /var/www/certbot

echo "=== [9/9] Certbot auto-renewal cron ==="
cat > /etc/cron.d/certbot-renew << 'CRON'
# Renew Let's Encrypt certs twice daily, reload nginx on success
0 */12 * * * root certbot renew --webroot -w /var/www/certbot --quiet --deploy-hook "docker exec mundi-nginx nginx -s reload" >> /var/log/certbot-renew.log 2>&1
CRON
chmod 644 /etc/cron.d/certbot-renew

echo ""
echo "========================================="
echo "  VPS setup complete!"
echo "========================================="
echo ""
echo "Next steps (as deploy user):"
echo "  su - ${DEPLOY_USER}"
echo "  cd ${REPO_DIR}"
echo ""
echo "  # 1. Create production env file"
echo "  cp .env.prod.example .env.prod"
echo "  nano .env.prod  # fill in real secrets"
echo ""
echo "  # 2. Build images"
echo "  docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env.prod build"
echo ""
echo "  # 3. Migrate data (see scripts/migrate-db.sh and scripts/migrate-s3.sh)"
echo ""
echo "  # 4. Start services"
echo "  docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env.prod up -d"
echo ""
echo "  # 5. Update Cloudflare DNS A records:"
echo "  #    gis.nozalabs.rw     → $(curl -s ifconfig.me)"
echo "  #    s3.gis.nozalabs.rw  → $(curl -s ifconfig.me)"
echo "  #    Proxy: OFF (grey cloud) for Let's Encrypt"
echo "  #    TTL: 300 (5 min)"
