#!/usr/bin/env bash
# ------------------------------------------------------------------
# Migrate Render PostgreSQL → local Docker PostgreSQL
#
# Usage:
#   RENDER_DB_URL="postgres://user:pass@host:5432/dbname" bash scripts/migrate-db.sh
#
# Prerequisites:
#   - pg_dump installed on the VPS (apt install postgresql-client-15)
#   - Local PostgreSQL container running
#   - .env.prod filled in
# ------------------------------------------------------------------
set -euo pipefail

RENDER_DB_URL="${RENDER_DB_URL:?Error: Set RENDER_DB_URL to your Render external database URL}"
DUMP_FILE="/tmp/render_dump_$(date -u +%Y%m%d_%H%M%S).sql"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env.prod"

# Read local DB credentials
if [ -f .env.prod ]; then
  PGUSER=$(grep -E '^POSTGRES_USER=' .env.prod | cut -d= -f2)
  PGDB=$(grep -E '^POSTGRES_DB=' .env.prod | cut -d= -f2)
fi
PGUSER="${PGUSER:-mundiuser}"
PGDB="${PGDB:-mundidb}"

echo "=== [1/4] Dumping Render database ==="
echo "Source: ${RENDER_DB_URL%%@*}@***"
pg_dump "$RENDER_DB_URL" \
  --no-owner \
  --no-acl \
  --format=plain \
  > "$DUMP_FILE"

DUMP_SIZE=$(du -h "$DUMP_FILE" | cut -f1)
echo "Dump complete: ${DUMP_FILE} (${DUMP_SIZE})"

echo "=== [2/4] Ensuring local PostgreSQL is running ==="
$COMPOSE up -d postgresdb
echo "Waiting for healthcheck..."
until $COMPOSE exec -T postgresdb pg_isready -U "$PGUSER" -d "$PGDB" > /dev/null 2>&1; do
  sleep 1
done
echo "PostgreSQL is ready"

echo "=== [3/4] Restoring into local PostgreSQL ==="
$COMPOSE exec -T postgresdb psql -U "$PGUSER" -d "$PGDB" < "$DUMP_FILE"
echo "Restore complete"

echo "=== [4/4] Verification ==="
echo ""
echo "--- Render table row counts (top 20) ---"
psql "$RENDER_DB_URL" -c "
  SELECT schemaname, relname AS table_name, n_live_tup AS rows
  FROM pg_stat_user_tables
  ORDER BY n_live_tup DESC
  LIMIT 20;
"

echo ""
echo "--- Local table row counts (top 20) ---"
$COMPOSE exec -T postgresdb psql -U "$PGUSER" -d "$PGDB" -c "
  SELECT schemaname, relname AS table_name, n_live_tup AS rows
  FROM pg_stat_user_tables
  ORDER BY n_live_tup DESC
  LIMIT 20;
"

rm -f "$DUMP_FILE"
echo ""
echo "=== Database migration complete ==="
echo "Compare the row counts above to verify data integrity."
