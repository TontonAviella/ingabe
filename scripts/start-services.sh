#!/bin/bash
# Start the mundi.ai app (8000) after migrations and the Hermes plugin install.

# -e exit on error, -o pipefail propagate failures through pipes (so a failure
# in `cmd | sed` actually fails the script). NOT using -u (unset vars) because
# the existing entrypoint references several env vars without defaults and
# adding -u risks breaking boot in unexpected configurations.
set -eo pipefail

echo "[start-services] Running alembic migrations..."
alembic upgrade head

# Hermes runs in this same app container. Generate one local HMAC secret in the
# private Ingabe cache volume when the operator has not supplied one. Reusing
# it across worker processes and restarts keeps the proxy and callback verifier
# in lockstep without committing a credential to the repository.
# Same values as hermes_runtime.hermes_is_enabled (case-insensitive).
case "$(printf '%s' "${MUNDI_USE_HERMES:-0}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|auto)
    if [ -z "${HERMES_GATEWAY_SECRET:-}" ]; then
      HERMES_LOCAL_SECRET_FILE="${HERMES_LOCAL_SECRET_FILE:-/tmp/ingabe_cache/hermes_gateway_secret}"
      if [ ! -s "$HERMES_LOCAL_SECRET_FILE" ]; then
        # Subshell: the restrictive umask must not leak into the services
        # started below, or their files differ between first and later boots.
        ( umask 077
          python -c 'import secrets; print(secrets.token_hex(32))' \
            > "${HERMES_LOCAL_SECRET_FILE}.tmp" )
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

echo "[start-services] Starting main app on :8000..."
exec uvicorn src.wsgi:app --host 0.0.0.0 --port 8000 --log-level debug --access-log --use-colors --proxy-headers --forwarded-allow-ips='*' --workers ${UVICORN_WORKERS:-1}
