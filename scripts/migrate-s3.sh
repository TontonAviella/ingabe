#!/usr/bin/env bash
# ------------------------------------------------------------------
# Migrate Cloudflare R2 → local MinIO via rclone (Docker)
#
# Usage:
#   R2_ACCOUNT_ID=xxx R2_ACCESS_KEY=xxx R2_SECRET_KEY=xxx bash scripts/migrate-s3.sh
#
# Runs rclone inside a Docker container on the compose network
# so it can reach MinIO via the service name (no port exposure needed).
# ------------------------------------------------------------------
set -euo pipefail

R2_ACCOUNT_ID="${R2_ACCOUNT_ID:?Error: Set R2_ACCOUNT_ID (Cloudflare account ID)}"
R2_ACCESS_KEY="${R2_ACCESS_KEY:?Error: Set R2_ACCESS_KEY (R2 access key)}"
R2_SECRET_KEY="${R2_SECRET_KEY:?Error: Set R2_SECRET_KEY (R2 secret key)}"
R2_BUCKET="${R2_BUCKET:-noza}"

# Read MinIO credentials from .env.prod
if [ -f .env.prod ]; then
  S3_ACCESS_KEY_ID=$(grep -E '^S3_ACCESS_KEY_ID=' .env.prod | cut -d= -f2)
  S3_SECRET_ACCESS_KEY=$(grep -E '^S3_SECRET_ACCESS_KEY=' .env.prod | cut -d= -f2)
  S3_BUCKET=$(grep -E '^S3_BUCKET=' .env.prod | cut -d= -f2)
fi
S3_ACCESS_KEY_ID="${S3_ACCESS_KEY_ID:-s3user}"
S3_SECRET_ACCESS_KEY="${S3_SECRET_ACCESS_KEY:-changeme}"
LOCAL_BUCKET="${S3_BUCKET:-noza}"

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env.prod"

echo "=== [1/3] Ensure MinIO is running ==="
$COMPOSE up -d minio mc
echo "Waiting for MinIO healthcheck..."
until $COMPOSE exec -T minio curl -sf http://localhost:9000/minio/health/live > /dev/null 2>&1; do
  sleep 1
done
echo "MinIO is ready"

echo "=== [2/3] Detect Docker network ==="
MINIO_CONTAINER=$($COMPOSE ps -q minio)
NETWORK=$(docker inspect "$MINIO_CONTAINER" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}')
echo "Using network: ${NETWORK}"

echo "=== [3/3] Sync R2 → MinIO via rclone ==="
echo "Source:      r2:${R2_BUCKET}"
echo "Destination: minio:${LOCAL_BUCKET}"
echo ""

docker run --rm \
  --network "$NETWORK" \
  -e "RCLONE_CONFIG_R2_TYPE=s3" \
  -e "RCLONE_CONFIG_R2_PROVIDER=Cloudflare" \
  -e "RCLONE_CONFIG_R2_ACCESS_KEY_ID=${R2_ACCESS_KEY}" \
  -e "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY=${R2_SECRET_KEY}" \
  -e "RCLONE_CONFIG_R2_ENDPOINT=https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com" \
  -e "RCLONE_CONFIG_MINIO_TYPE=s3" \
  -e "RCLONE_CONFIG_MINIO_PROVIDER=Minio" \
  -e "RCLONE_CONFIG_MINIO_ACCESS_KEY_ID=${S3_ACCESS_KEY_ID}" \
  -e "RCLONE_CONFIG_MINIO_SECRET_ACCESS_KEY=${S3_SECRET_ACCESS_KEY}" \
  -e "RCLONE_CONFIG_MINIO_ENDPOINT=http://minio:9000" \
  rclone/rclone sync "r2:${R2_BUCKET}" "minio:${LOCAL_BUCKET}" \
    --progress \
    --transfers 8 \
    --checkers 16 \
    --fast-list

echo ""
echo "=== Verification ==="
echo "--- Source size (R2) ---"
docker run --rm \
  -e "RCLONE_CONFIG_R2_TYPE=s3" \
  -e "RCLONE_CONFIG_R2_PROVIDER=Cloudflare" \
  -e "RCLONE_CONFIG_R2_ACCESS_KEY_ID=${R2_ACCESS_KEY}" \
  -e "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY=${R2_SECRET_KEY}" \
  -e "RCLONE_CONFIG_R2_ENDPOINT=https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com" \
  rclone/rclone size "r2:${R2_BUCKET}"

echo ""
echo "--- Destination size (MinIO) ---"
docker run --rm \
  --network "$NETWORK" \
  -e "RCLONE_CONFIG_MINIO_TYPE=s3" \
  -e "RCLONE_CONFIG_MINIO_PROVIDER=Minio" \
  -e "RCLONE_CONFIG_MINIO_ACCESS_KEY_ID=${S3_ACCESS_KEY_ID}" \
  -e "RCLONE_CONFIG_MINIO_SECRET_ACCESS_KEY=${S3_SECRET_ACCESS_KEY}" \
  -e "RCLONE_CONFIG_MINIO_ENDPOINT=http://minio:9000" \
  rclone/rclone size "minio:${LOCAL_BUCKET}"

echo ""
echo "=== S3 migration complete ==="
echo "Compare the file counts and sizes above to verify."
echo ""
echo "To re-run an incremental sync before DNS cutover:"
echo "  R2_ACCOUNT_ID=xxx R2_ACCESS_KEY=xxx R2_SECRET_KEY=xxx bash scripts/migrate-s3.sh"
