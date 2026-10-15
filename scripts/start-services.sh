#!/bin/bash
# Start the mundi.ai app (8000) after migrations.

# -e exit on error, -o pipefail propagate failures through pipes (so a failure
# in `cmd | sed` actually fails the script). NOT using -u (unset vars) because
# the existing entrypoint references several env vars without defaults and
# adding -u risks breaking boot in unexpected configurations.
set -eo pipefail

echo "[start-services] Running alembic migrations..."
alembic upgrade head

echo "[start-services] Starting main app on :8000..."
exec uvicorn src.wsgi:app --host 0.0.0.0 --port 8000 --log-level ${UVICORN_LOG_LEVEL:-info} --access-log --use-colors --proxy-headers --forwarded-allow-ips='*' --workers ${UVICORN_WORKERS:-1}
