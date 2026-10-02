#!/bin/bash
# Start all mundi.ai services: main app (8000), field monitor (8001), insurance (8002)
# Main app runs in foreground; satellite APIs run as background processes with auto-restart.

# -e exit on error, -o pipefail propagate failures through pipes (so a failure
# in `cmd | sed` actually fails the script). NOT using -u (unset vars) because
# the existing entrypoint references several env vars without defaults and
# adding -u risks breaking boot in unexpected configurations.
set -eo pipefail

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

# Hermes runs in this same app container. Generate one local HMAC secret in the
# private Ingabe cache volume when the operator has not supplied one. Reusing
# it across worker processes and restarts keeps the proxy and callback verifier
# in lockstep without committing a credential to the repository.
case "${MUNDI_USE_HERMES:-auto}" in
  1|true|TRUE|yes|YES|auto)
    if [ -z "${HERMES_GATEWAY_SECRET:-}" ]; then
      HERMES_LOCAL_SECRET_FILE="${HERMES_LOCAL_SECRET_FILE:-/tmp/ingabe_cache/hermes_gateway_secret}"
      if [ ! -s "$HERMES_LOCAL_SECRET_FILE" ]; then
        umask 077
        python -c 'import secrets; print(secrets.token_hex(32))' \
          > "${HERMES_LOCAL_SECRET_FILE}.tmp"
        mv -f "${HERMES_LOCAL_SECRET_FILE}.tmp" "$HERMES_LOCAL_SECRET_FILE"
        echo "[start-services] Generated a private local Hermes tool secret"
      fi
      export HERMES_GATEWAY_SECRET="$(cat "$HERMES_LOCAL_SECRET_FILE")"
    fi
    ;;
esac

if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -n "${OPENAI_API_KEY:-}" ]; then
  case "${OPENAI_BASE_URL:-}" in
    *openrouter.ai*) export OPENROUTER_API_KEY="$OPENAI_API_KEY" ;;
  esac
fi

# --- Hermes plugin install (idempotent, see scripts/install-hermes-plugin.sh) ----
# The local Docker app always reaches this path through docker-compose.yml.
bash /app/scripts/install-hermes-plugin.sh

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
exec uvicorn src.wsgi:app --host 0.0.0.0 --port 8000 --log-level debug --access-log --use-colors --proxy-headers --forwarded-allow-ips='*' --workers ${UVICORN_WORKERS:-1}
