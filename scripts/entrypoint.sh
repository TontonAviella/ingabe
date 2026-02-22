#!/bin/bash
set -e

SERVICE="${RENDER_SERVICE:-app}"

case "$SERVICE" in
  app)
    echo "Running Alembic migrations..."
    alembic upgrade head
    echo "Starting app server..."
    exec uvicorn src.wsgi:app --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  dagster-webserver)
    echo "Starting Dagster webserver..."
    exec dagster-webserver -h 0.0.0.0 -p "${PORT:-3000}" -w /app/dagster_home/workspace.yaml
    ;;
  dagster-daemon)
    echo "Starting Dagster daemon..."
    exec dagster-daemon run -w /app/dagster_home/workspace.yaml
    ;;
  *)
    echo "Unknown service: $SERVICE"
    exit 1
    ;;
esac
