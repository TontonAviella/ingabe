#!/bin/bash
set -e

SERVICE="${RENDER_SERVICE:-app}"

echo "=== Entrypoint starting: SERVICE=$SERVICE ==="
echo "=== PORT=${PORT:-8000} ==="
echo "=== POSTGRES_HOST=${POSTGRES_HOST:-NOT SET} ==="
echo "=== DATABASE_URL set: $([ -n "$DATABASE_URL" ] && echo 'yes' || echo 'no') ==="
echo "=== MUNDI_AUTH_MODE=${MUNDI_AUTH_MODE:-NOT SET} ==="
echo "=== CLERK_SECRET_KEY set: $([ -n "$CLERK_SECRET_KEY" ] && echo 'yes' || echo 'no') ==="

case "$SERVICE" in
  app)
    echo "Ensuring PostGIS extension exists..."
    python3 -c "
import os, psycopg2
conn = psycopg2.connect(
    host=os.environ['POSTGRES_HOST'],
    port=os.environ.get('POSTGRES_PORT', '5432'),
    dbname=os.environ['POSTGRES_DB'],
    user=os.environ['POSTGRES_USER'],
    password=os.environ['POSTGRES_PASSWORD'],
)
conn.autocommit = True
conn.cursor().execute('CREATE EXTENSION IF NOT EXISTS postgis')
conn.close()
print('PostGIS extension ready')
" || echo "Warning: could not enable PostGIS (may already exist)"

    echo "Running Alembic migrations..."
    if alembic upgrade head; then
      echo "Alembic migrations completed successfully."
    else
      echo "WARNING: Alembic migrations failed (exit $?). Continuing anyway — migrations may already be applied."
    fi

    echo "Starting app server on port ${PORT:-8000}..."
    exec uvicorn src.wsgi:app --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  dagster-daemon)
    echo "Starting Dagster daemon..."
    exec dagster-daemon run -w /app/workspace.yaml
    ;;
  *)
    echo "Unknown service: $SERVICE"
    exit 1
    ;;
esac
