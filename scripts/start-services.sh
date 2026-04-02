#!/bin/bash
# Start all mundi.ai services: main app (8000), field monitor (8001), insurance (8002)
# Main app runs in foreground; satellite APIs run as background processes with auto-restart.

set -e

echo "[start-services] Installing dependencies..."
# Bootstrap pip if missing (common in slim containers)
if ! python -m pip --version 2>/dev/null; then
  echo "[start-services] Bootstrapping pip..."
  python -c "import urllib.request; urllib.request.urlretrieve('https://bootstrap.pypa.io/get-pip.py', '/tmp/get-pip.py')"
  python /tmp/get-pip.py --quiet 2>&1 | tail -3
fi
# Install satellite + memory deps
python -m pip install --quiet planetary-computer pystac-client 2>&1 | tail -5
echo "[start-services] Dependencies ready"

echo "[start-services] Running alembic migrations..."
alembic upgrade head

echo "[start-services] Starting field monitor API on :8001..."
(
  while true; do
    python -m uvicorn api_monitor:app --host 0.0.0.0 --port 8001 --log-level warning 2>&1 | sed 's/^/[monitor] /'
    echo "[monitor] Process exited ($?), restarting in 3s..."
    sleep 3
  done
) &

echo "[start-services] Starting insurance API on :8002..."
(
  while true; do
    python -m uvicorn api_insurance:app --host 0.0.0.0 --port 8002 --log-level warning 2>&1 | sed 's/^/[insurance] /'
    echo "[insurance] Process exited ($?), restarting in 3s..."
    sleep 3
  done
) &

echo "[start-services] Starting main app on :8000..."
exec uvicorn src.wsgi:app --host 0.0.0.0 --port 8000 --log-level debug --access-log --use-colors
