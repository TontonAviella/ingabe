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

METRICS_DIR="${BACKUP_METRICS_DIR:-}"

# Health metrics for Prometheus (node-exporter's textfile collector reads ${METRICS_DIR}/backup.prom): when the
# last run ended and whether it worked, when the last good backup was made and its size, and the size of the one
# before it. A failed run keeps the last good values. Off when BACKUP_METRICS_DIR is unset.
previous_metric() { grep "^$1 " "${METRICS_DIR}/backup.prom" 2>/dev/null | awk '{print $2}' || true; }
write_metrics() {  # $1 = 1 (worked) or 0 (failed); $2 = size in bytes of the new backup (when it worked)
  [ -n "${METRICS_DIR}" ] && [ -d "${METRICS_DIR}" ] || return 0
  local now last_ok size prev_size
  now=$(date -u +%s)
  if [ "$1" = 1 ]; then
    last_ok="${now}"; size="$2"; prev_size="$(previous_metric backup_last_size_bytes)"
  else
    last_ok="$(previous_metric backup_last_success_timestamp_seconds)"
    size="$(previous_metric backup_last_size_bytes)"; prev_size="$(previous_metric backup_previous_size_bytes)"
  fi
  {
    echo "# HELP backup_last_run_timestamp_seconds When the last backup run ended"
    echo "# TYPE backup_last_run_timestamp_seconds gauge"
    echo "backup_last_run_timestamp_seconds ${now}"
    echo "# HELP backup_last_run_success 1 when the last backup run worked, 0 when it failed"
    echo "# TYPE backup_last_run_success gauge"
    echo "backup_last_run_success $1"
    echo "# HELP backup_last_success_timestamp_seconds When the last good backup was made"
    echo "# TYPE backup_last_success_timestamp_seconds gauge"
    echo "backup_last_success_timestamp_seconds ${last_ok:-0}"
    echo "# HELP backup_last_size_bytes Compressed size of the last good backup"
    echo "# TYPE backup_last_size_bytes gauge"
    echo "backup_last_size_bytes ${size:-0}"
    echo "# HELP backup_previous_size_bytes Compressed size of the good backup before it"
    echo "# TYPE backup_previous_size_bytes gauge"
    echo "backup_previous_size_bytes ${prev_size:-${size:-0}}"
  } > "${METRICS_DIR}/backup.prom.tmp" && mv "${METRICS_DIR}/backup.prom.tmp" "${METRICS_DIR}/backup.prom"
}
trap 'write_metrics 0' ERR

echo "[backup] Starting pg_dump of ${DB_NAME} at ${TIMESTAMP}"

# 1. Dump + compress
# The brain/map tables FORCE row-level security, so pg_dump as the app user
# fails without --enable-row-security. With it, the app user with no
# app.user_id / app.partner_id set sees every row (the policies' admin
# context): checked 2026-10-05, identical row counts to the superuser on all
# 24 forced-RLS tables. If a policy ever hides rows from that context, this
# backup would silently miss them; re-check after RLS policy changes.
PGPASSWORD="${POSTGRES_PASSWORD:-changeme}" pg_dump \
  -h "${POSTGRES_HOST:-postgresdb}" \
  -p "${POSTGRES_PORT:-5432}" \
  -U "${POSTGRES_USER:-mundiuser}" \
  -d "${DB_NAME}" \
  --no-owner \
  --no-privileges \
  --enable-row-security \
  --format=plain \
  | gzip > "${DUMP_FILE}"

DUMP_SIZE=$(du -h "${DUMP_FILE}" | cut -f1)
DUMP_BYTES=$(wc -c < "${DUMP_FILE}" | tr -d ' ')
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
# The backup is stored: record it, so a failed prune below does not read as a failed backup.
write_metrics 1 "${DUMP_BYTES}"
trap - ERR

# 4. Clean up local dump
rm -f "${DUMP_FILE}"

# 5. Prune old backups
echo "[backup] Pruning backups older than ${RETENTION_DAYS} days"
# Epoch arithmetic: busybox date (alpine) cannot parse "-7 days".
CUTOFF=$(date -u -d "@$(( $(date -u +%s) - RETENTION_DAYS * 86400 ))" +"%Y-%m-%dT" 2>/dev/null || echo "")

if [ -n "${CUTOFF}" ]; then
  mc ls "mundi/${S3_BUCKET:-test-bucket}/backups/" 2>/dev/null | while read -r line; do
    FILENAME=$(echo "${line}" | awk '{print $NF}')
    # Extract date from filename: mundidb_2026-02-22T020000Z.sql.gz
    FILE_DATE=$(echo "${FILENAME}" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}T' || echo "")
    if [ -n "${FILE_DATE}" ] && [ "${FILE_DATE}" \< "${CUTOFF}" ]; then
      echo "[backup] Removing old backup: ${FILENAME}"
      mc rm "mundi/${S3_BUCKET:-test-bucket}/backups/${FILENAME}"
    fi
  done
fi

echo "[backup] Done at $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
