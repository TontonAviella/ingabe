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

# --- Hermes plugin install (no-op if hermes_integration/ isn't baked in) ----
# When MUNDI_USE_HERMES=1 the in-process AIAgent path needs the ingabe-sage
# plugin discoverable + enabled. Symlink the baked-in plugin path into
# Hermes's user-plugin search dir and seed a minimal config.yaml. Idempotent
# on restart. Safe to run when MUNDI_USE_HERMES=0 — the symlink just sits
# there unused.
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
if [ -d /app/hermes_integration/plugins/ingabe-sage ]; then
  mkdir -p "$HERMES_HOME/plugins"
  ln -sfn /app/hermes_integration/plugins/ingabe-sage "$HERMES_HOME/plugins/ingabe-sage"
  if [ ! -f "$HERMES_HOME/config.yaml" ]; then
    # Ships the full Hermes default toolset (browser, terminal,
    # code_execution, cronjob, delegation, file, image_gen, memory,
    # session_search, skills, todo, vision, web) alongside our plugin.
    # Roger explicitly chose to keep these on (2026-05-18) even though
    # the chat-only Sage UX doesn't currently use them — they're cheap
    # to keep around for future features (cron-fired reports, delegation,
    # memory-backed long conversations). If profile work later shows the
    # full surface is hurting Nemotron latency, add an explicit
    # `platform_toolsets.api_server: [ingabe-sage, ingabe-sage-proxied]`
    # block here. See memory: project_hermes_tool_surface_trim for the
    # opt-in trim.
    cat > "$HERMES_HOME/config.yaml" <<EOF
plugins:
  enabled:
    - ingabe-sage
model:
  provider: openrouter
  default: ${OPENAI_MODEL:-nvidia/nemotron-3-super-120b-a12b:free}
  base_url: ${OPENAI_BASE_URL:-https://openrouter.ai/api/v1}
EOF
    echo "[start-services] Seeded $HERMES_HOME/config.yaml (ingabe-sage enabled, default tool surface kept)"
  fi
  echo "[start-services] Hermes plugin ingabe-sage wired ($(readlink $HERMES_HOME/plugins/ingabe-sage))"
fi

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
exec uvicorn src.wsgi:app --host 0.0.0.0 --port 8000 --log-level debug --access-log --use-colors --proxy-headers --forwarded-allow-ips='*' --workers ${UVICORN_WORKERS:-4}
