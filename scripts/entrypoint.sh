#!/bin/bash
set -e

SERVICE="${RENDER_SERVICE:-app}"

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
    alembic upgrade head
    echo "Starting app server..."
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
