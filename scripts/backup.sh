#!/usr/bin/env bash
# ------------------------------------------------------------------
# Mundi.ai PostgreSQL backup script
#
# Dumps the database, gzips it, uploads to S3/MinIO, and prunes
# backups older than BACKUP_RETENTION_DAYS.
#
# Expected environment variables (all have sensible defaults):
#   POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD
#   S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_BUCKET
#   BACKUP_RETENTION_DAYS  (default: 7)
# ------------------------------------------------------------------
set -euo pipefail

TIMESTAMP=$(date -u +"%Y-%m-%dT%H%M%SZ")
DB_NAME="${POSTGRES_DB:-mundidb}"
DUMP_FILE="/tmp/${DB_NAME}_${TIMESTAMP}.sql.gz"
S3_PATH="s3://${S3_BUCKET:-test-bucket}/backups/${DB_NAME}_${TIMESTAMP}.sql.gz"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"

echo "[backup] Starting pg_dump of ${DB_NAME} at ${TIMESTAMP}"

# 1. Dump + compress
PGPASSWORD="${POSTGRES_PASSWORD:-changeme}" pg_dump \
  -h "${POSTGRES_HOST:-postgresdb}" \
  -p "${POSTGRES_PORT:-5432}" \
  -U "${POSTGRES_USER:-mundiuser}" \
  -d "${DB_NAME}" \
  --no-owner \
  --no-privileges \
  --format=plain \
  | gzip > "${DUMP_FILE}"

DUMP_SIZE=$(du -h "${DUMP_FILE}" | cut -f1)
echo "[backup] Dump complete: ${DUMP_FILE} (${DUMP_SIZE})"

# 2. Configure mc alias
mc alias set mundi \
  "${S3_ENDPOINT_URL:-http://minio:9000}" \
  "${S3_ACCESS_KEY_ID:-s3user}" \
  "${S3_SECRET_ACCESS_KEY:-changeme}" \
  --api S3v4 \
  > /dev/null 2>&1

# 3. Upload to S3
echo "[backup] Uploading to ${S3_PATH}"
mc cp "${DUMP_FILE}" "mundi/${S3_PATH#s3://}"

# 4. Clean up local dump
rm -f "${DUMP_FILE}"

# 5. Prune old backups
echo "[backup] Pruning backups older than ${RETENTION_DAYS} days"
CUTOFF=$(date -u -d "-${RETENTION_DAYS} days" +"%Y-%m-%dT" 2>/dev/null || \
         date -u -v-"${RETENTION_DAYS}"d +"%Y-%m-%dT" 2>/dev/null || echo "")

if [ -n "${CUTOFF}" ]; then
  mc ls "mundi/${S3_BUCKET:-test-bucket}/backups/" 2>/dev/null | while read -r line; do
    FILENAME=$(echo "${line}" | awk '{print $NF}')
    # Extract date from filename: mundidb_2026-02-22T020000Z.sql.gz
    FILE_DATE=$(echo "${FILENAME}" | grep -oP '\d{4}-\d{2}-\d{2}T' || echo "")
    if [ -n "${FILE_DATE}" ] && [ "${FILE_DATE}" \< "${CUTOFF}" ]; then
      echo "[backup] Removing old backup: ${FILENAME}"
      mc rm "mundi/${S3_BUCKET:-test-bucket}/backups/${FILENAME}"
    fi
  done
fi

echo "[backup] Done at $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
