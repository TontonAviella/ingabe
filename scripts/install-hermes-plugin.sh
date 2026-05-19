#!/bin/bash
# Idempotent Hermes plugin wiring for the in-process AIAgent runtime.
#
# Called from two places:
#   - scripts/start-services.sh — the in-image CMD chain (any compose
#     target that does NOT override `command:` inherits it)
#   - docker-compose.prod.yml `command:` — prod overrides `command:` to
#     skip the satellite APIs, so it calls this script directly before
#     alembic + uvicorn.
#
# Behavior:
#   - symlink at $HERMES_HOME/plugins/ingabe-sage is always refreshed
#   - config.yaml is rewritten atomically on every boot, so changes to
#     OPENAI_MODEL / OPENAI_BASE_URL in .env propagate on restart
#   - if MUNDI_USE_HERMES=1 AND plugin dir is missing, exit 1 to fail-fast
#     (misconfiguration shows up at container boot, not at first chat turn)
#   - if MUNDI_USE_HERMES is unset/0, missing plugin is a soft no-op
#
# Required env (read by Hermes runtime, NOT used here):
#   - MUNDI_USE_HERMES=1  to actually activate the in-process path
#   - OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL
#
# SECURITY: the model + base_url values are written into config.yaml. We
# validate them against a strict regex before writing and YAML-quote the
# substituted values to neutralize newlines, `#`, or shell metacharacters.
# Do NOT add lines that reference secret env vars (OPENAI_API_KEY,
# HERMES_GATEWAY_SECRET, CLERK_SECRET_KEY, etc.) — they would get baked
# into config.yaml on disk and persist across restarts.

set -eo pipefail

PLUGIN_NAME="${PLUGIN_NAME:-ingabe-sage}"
PLUGIN_SRC="${PLUGIN_SRC:-/app/hermes_integration/plugins/${PLUGIN_NAME}}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_FLAG="${MUNDI_USE_HERMES:-0}"

if [ ! -d "$PLUGIN_SRC" ]; then
  if [ "$HERMES_FLAG" = "1" ] || [ "$HERMES_FLAG" = "true" ]; then
    echo "[install-hermes-plugin] FATAL: MUNDI_USE_HERMES=$HERMES_FLAG but $PLUGIN_SRC missing" >&2
    echo "[install-hermes-plugin] The image was built without the plugin baked in." >&2
    exit 1
  fi
  echo "[install-hermes-plugin] $PLUGIN_SRC not found and MUNDI_USE_HERMES is off — skipping"
  exit 0
fi

mkdir -p "$HERMES_HOME/plugins"

# Always refresh the symlink. -sfn handles existing symlinks but blows up
# on a real directory at the target — clean that up first to be safe.
if [ -d "$HERMES_HOME/plugins/${PLUGIN_NAME}" ] && [ ! -L "$HERMES_HOME/plugins/${PLUGIN_NAME}" ]; then
  echo "[install-hermes-plugin] removing pre-existing real directory at $HERMES_HOME/plugins/${PLUGIN_NAME}" >&2
  rm -rf "$HERMES_HOME/plugins/${PLUGIN_NAME}"
fi
ln -sfn "$PLUGIN_SRC" "$HERMES_HOME/plugins/${PLUGIN_NAME}"

# Validate env-driven config values. Reject anything that could break YAML
# or inject shell metacharacters. Fall back to defaults on malformed input.
OPENAI_MODEL_DEFAULT="nvidia/nemotron-3-super-120b-a12b:free"
OPENAI_BASE_URL_DEFAULT="https://openrouter.ai/api/v1"
MODEL_RE='^[A-Za-z0-9._/:-]+$'
URL_RE='^https?://[A-Za-z0-9._/:-]+$'

MODEL_VAL="${OPENAI_MODEL:-$OPENAI_MODEL_DEFAULT}"
BASE_URL_VAL="${OPENAI_BASE_URL:-$OPENAI_BASE_URL_DEFAULT}"

if ! [[ "$MODEL_VAL" =~ $MODEL_RE ]]; then
  echo "[install-hermes-plugin] WARN: OPENAI_MODEL=$MODEL_VAL fails regex — using default" >&2
  MODEL_VAL="$OPENAI_MODEL_DEFAULT"
fi
if ! [[ "$BASE_URL_VAL" =~ $URL_RE ]]; then
  echo "[install-hermes-plugin] WARN: OPENAI_BASE_URL=$BASE_URL_VAL fails regex — using default" >&2
  BASE_URL_VAL="$OPENAI_BASE_URL_DEFAULT"
fi

# Atomic rewrite: write to .tmp, mv into place. Survives ENOSPC mid-write
# (the partial .tmp is discarded) and propagates env changes on every boot.
# Ships the full Hermes default toolset (browser, terminal, code_execution,
# cronjob, delegation, file, image_gen, memory, session_search, skills,
# todo, vision, web) alongside our plugin. See memory:
# project_hermes_tool_surface_trim for the opt-in trim.
# Auxiliary model: Hermes uses this cheaper model for context
# compression, summarization, and tool-routing decisions. Without it,
# the primary Nemotron Super 3 handles those operations too — costing
# attention budget on tasks it is over-spec for. Default is
# Nemotron-Nano-9B-v2 (free tier on OpenRouter); override via
# AUXILIARY_MODEL env var. Skipped when the env value is malformed
# (Hermes falls back to the primary in that case).
AUX_MODEL_VAL="${AUXILIARY_MODEL:-nvidia/nemotron-nano-9b-v2:free}"
if [[ ! "$AUX_MODEL_VAL" =~ $MODEL_RE ]]; then
  echo "[install-hermes-plugin] WARN: AUXILIARY_MODEL=$AUX_MODEL_VAL fails regex — skipping aux block" >&2
  AUX_BLOCK=""
else
  # Trailing newline is intentional: when the block is non-empty the
  # full string ends in \n so it concatenates cleanly into the YAML
  # content below.
  AUX_BLOCK="auxiliary:
  provider: openrouter
  model: \"${AUX_MODEL_VAL}\"
  base_url: \"${BASE_URL_VAL}\"
"
fi

# Build the full config as a single string, then write atomically.
# Avoids the heredoc-terminator pitfall when the AUX_BLOCK expansion
# would have put EOF on a non-empty source line.
CONFIG_CONTENT="plugins:
  enabled:
    - ${PLUGIN_NAME}
model:
  provider: openrouter
  default: \"${MODEL_VAL}\"
  base_url: \"${BASE_URL_VAL}\"
${AUX_BLOCK}"
CONFIG_TMP="$HERMES_HOME/config.yaml.tmp"
printf "%s" "$CONFIG_CONTENT" > "$CONFIG_TMP"
mv -f "$CONFIG_TMP" "$HERMES_HOME/config.yaml"

echo "[install-hermes-plugin] Wired $HERMES_HOME/plugins/${PLUGIN_NAME} -> $(readlink "$HERMES_HOME/plugins/${PLUGIN_NAME}")"
echo "[install-hermes-plugin] Rewrote $HERMES_HOME/config.yaml (model=$MODEL_VAL)"
