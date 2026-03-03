#!/bin/bash

SERVICE="${RENDER_SERVICE:-app}"

echo "=== Entrypoint starting: SERVICE=$SERVICE ==="
echo "=== PORT=${PORT:-8000} ==="
echo "=== POSTGRES_HOST=${POSTGRES_HOST:-NOT SET} ==="
echo "=== MUNDI_AUTH_MODE=${MUNDI_AUTH_MODE:-NOT SET} ==="
echo "=== Python: $(python3 --version 2>&1) ==="

case "$SERVICE" in
  app)
    # Step 1: PostGIS extension (non-fatal)
    echo "Step 1: Ensuring PostGIS extension exists..."
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
" && echo "Step 1: OK" || echo "Step 1: WARNING - PostGIS check failed (continuing)"

    # Step 2: Alembic migrations (non-fatal)
    echo "Step 2: Running Alembic migrations..."
    alembic upgrade head && echo "Step 2: OK" || echo "Step 2: WARNING - Alembic failed (continuing)"

    # Step 3: Verify Python imports work
    echo "Step 3: Testing Python imports..."
    python3 -c "
import sys
try:
    print('  Importing src.wsgi...')
    import src.wsgi
    print('  src.wsgi imported OK')
    print('  App routes:', len(src.wsgi.app.routes))
except Exception as e:
    print(f'  IMPORT ERROR: {type(e).__name__}: {e}', file=sys.stderr)
    sys.exit(1)
" && echo "Step 3: OK" || { echo "Step 3: FAILED - app import error"; exit 1; }

    # Step 4: Start uvicorn
    WORKERS="${WEB_CONCURRENCY:-4}"
    echo "Step 4: Starting uvicorn on port ${PORT:-8000} with $WORKERS workers..."
    exec uvicorn src.wsgi:app --host 0.0.0.0 --port "${PORT:-8000}" --workers "$WORKERS" --log-level info
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
