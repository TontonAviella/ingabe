#!/usr/bin/env bash
# A second copy of the app for the browser tests, beside the one you use: nobody signs in to it, it has its own
# database (never mundidb) and it serves this checkout's code and this checkout's frontend.
#
#   scripts/e2e_app.sh up      build the frontend without sign-in, start the copy on :8011, seed the test farm
#   scripts/e2e_app.sh seed    seed the test farm again (scripts/e2e_seed.py)
#   scripts/e2e_app.sh test    run the browser tests against the copy (extra arguments go to Playwright)
#   scripts/e2e_app.sh down    stop and remove the copy (its database is kept for the next run)
#
# Why a copy: the sign-in provider is fixed when the frontend is built (VITE_AUTH_PROVIDER), and the app on :8000
# is built with WorkOS and reads the live local database. A browser test can use neither.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="${E2E_CONTAINER:-mundi-app-e2e}"
PORT="${E2E_PORT:-8011}"
DATABASE="${E2E_DATABASE:-mundidb_e2e}"
TEMPLATE="${E2E_TEMPLATE_DATABASE:-mundidb_pytest_wos}"
PROJECT="${E2E_COMPOSE_PROJECT:-mundiai}"
POSTGRES="${E2E_POSTGRES_CONTAINER:-mundiai-postgresdb-1}"
# The stack's settings (database password, storage keys) live in the main checkout's .env; a worktree has none.
ENV_FILE="${E2E_ENV_FILE:-$(cd "$(git -C "$ROOT" rev-parse --git-common-dir)/.." && pwd)/.env}"

if [ "$DATABASE" = "mundidb" ]; then
  echo "E2E_DATABASE=mundidb is the live database: refusing." >&2
  exit 1
fi

seed() {
  docker exec "$NAME" python scripts/e2e_seed.py
}

case "${1:-}" in
  up)
    if ! docker exec "$POSTGRES" sh -c 'psql -U "$POSTGRES_USER" -d postgres -Atc "select 1 from pg_database where datname = '"'$DATABASE'"'"' | grep -q 1; then
      docker exec "$POSTGRES" sh -c 'createdb -U "$POSTGRES_USER" -T '"$TEMPLATE $DATABASE"
      echo "[e2e] made database $DATABASE from $TEMPLATE"
    fi
    echo "[e2e] building the frontend without sign-in..."
    (cd "$ROOT/frontendts" && VITE_AUTH_PROVIDER= VITE_POSTHOG_KEY= npm run build >/dev/null)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    # No sign-in provider, its own database, the field switch on, and no model key: a browser test never pays
    # for a model call.
    (cd "$ROOT" && docker compose -p "$PROJECT" --env-file "$ENV_FILE" run -d --no-deps --name "$NAME" \
      -p "$PORT:8000" -e AUTH_PROVIDER= -e MUNDI_AUTH_MODE=edit -e POSTGRES_DB="$DATABASE" -e FIELD_TAPS=1 \
      -e OPENAI_API_KEY= -e NTFY_TOPIC= -e OTEL_EXPORTER_OTLP_ENDPOINT= \
      -v "$ROOT/frontendts/dist:/app/frontendts/dist:ro" app >/dev/null)
    for _ in $(seq 1 90); do
      if curl -sf "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
        echo "[e2e] the copy answers on http://localhost:$PORT"
        seed
        exit 0
      fi
      sleep 2
    done
    echo "[e2e] the copy did not answer within 3 minutes; its last lines:" >&2
    docker logs --tail 40 "$NAME" >&2 || true
    exit 1
    ;;
  seed)
    seed
    ;;
  test)
    shift
    (cd "$ROOT/frontendts" && E2E_BASE_URL="http://localhost:$PORT" npx playwright test "$@")
    ;;
  down)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    echo "[e2e] the copy is removed; database $DATABASE is kept"
    ;;
  *)
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
