#!/usr/bin/env bash
# Reproducible smoke for Patterns A+B+C+D against a running docker stack.
#
# Pattern A — active-memory injection: INGABE_PATTERN_A_ACTIVE_MEMORY default on,
#             brain_service importable.
# Pattern B — tool-description disambiguation: pattern_b_annotate_tools.py is
#             idempotent and 12 target tools carry the "WHEN TO USE:" sentinel.
# Pattern C — ambient channels: redis-published mundi:render_snapshot payload is
#             received by both senders in log-only stub mode.
# Pattern D — runtime-bound composition: INGABE_PATTERN_D_COMPOSITION default on,
#             INGABE_PATTERN_D_TOOL_TIMEOUT_SEC default 60.
#
# Container names are configurable via env so this runs unchanged in CI:
#   APP_CONTAINER, TELEGRAM_CONTAINER, WHATSAPP_CONTAINER.
# The redis used for the Pattern C publish is auto-discovered from the
# telegram sender's network (so we hit the SAME redis the senders are
# subscribed to, not whatever redis happens to share a name).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

APP_CONTAINER="${APP_CONTAINER:-mundi-app}"
TELEGRAM_CONTAINER="${TELEGRAM_CONTAINER:-mundi-telegram-sender}"
WHATSAPP_CONTAINER="${WHATSAPP_CONTAINER:-mundi-whatsapp-sender}"

PASS=0
FAIL=0

step() { printf '\n=== %s ===\n' "$1"; }
ok()   { printf '  [PASS] %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL+1)); }

require_running() {
  local name="$1"
  if [ -z "$(docker ps -q --filter "name=^${name}$")" ]; then
    bad "container '$name' is not running"
    return 1
  fi
  return 0
}

# ---------- preflight ----------
step "preflight"
require_running "$APP_CONTAINER"      || exit 2
require_running "$TELEGRAM_CONTAINER" || exit 2
require_running "$WHATSAPP_CONTAINER" || exit 2
ok "app + both senders running"

# Discover the redis the senders are actually wired to (REDIS_HOST is the
# DNS name; we resolve it through the sender's network).
SENDER_NET="$(docker inspect "$TELEGRAM_CONTAINER" \
  --format '{{range $k, $_ := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' \
  | head -1)"
REDIS_CONTAINER="$(docker network inspect "$SENDER_NET" \
  --format '{{range .Containers}}{{.Name}}{{"\n"}}{{end}}' \
  | grep -E 'redis' | head -1)"
if [ -z "$REDIS_CONTAINER" ]; then
  bad "could not find redis on network '$SENDER_NET'"
  exit 2
fi
ok "redis on sender network: $REDIS_CONTAINER"

# ---------- Pattern A ----------
step "Pattern A — active-memory injection"
if docker exec "$APP_CONTAINER" python -c "
import os, importlib
assert os.environ.get('INGABE_PATTERN_A_ACTIVE_MEMORY', '1') == '1', 'A disabled by env'
importlib.import_module('src.services.brain_service')
" >/dev/null 2>&1; then
  ok "INGABE_PATTERN_A_ACTIVE_MEMORY defaults on; brain_service importable"
else
  bad "Pattern A precondition failed (env or brain_service import)"
fi

# ---------- Pattern B ----------
# Annotator is a build-time tool — runs from the host against the working tree.
# The deployed image must carry the already-annotated tools.json; we verify the
# sentinel count inside the running container as the deployment gate.
step "Pattern B — tool-description disambiguation"

# Host-side idempotency check: run the annotator twice against the local copy.
# Captures both the "script works" and "running twice doesn't double-annotate"
# properties without mutating the live container.
B_OUT="$(python3 "$REPO_ROOT/scripts/pattern_b_annotate_tools.py" 2>&1)"
B_RC=$?
if [ "$B_RC" -eq 0 ]; then
  ok "annotator exits 0 (host): $B_OUT"
else
  bad "annotator exit=$B_RC (host): $B_OUT"
fi

B_OUT2="$(python3 "$REPO_ROOT/scripts/pattern_b_annotate_tools.py" 2>&1)"
case "$B_OUT2" in
  *"annotated=0"*) ok "annotator idempotent (host): $B_OUT2" ;;
  *)               bad "annotator NOT idempotent (host): $B_OUT2" ;;
esac

# Deployment gate: deployed image must have the annotated tools.json baked in.
SENT_COUNT="$(docker exec "$APP_CONTAINER" python -c "
import json
data = json.load(open('/app/src/geoprocessing/tools.json'))
n = sum(1 for e in data if isinstance(e, dict) and ' WHEN TO USE:' in (e.get('function', {}).get('description') or ''))
print(n)
" 2>/dev/null)"
if [ "$SENT_COUNT" = "12" ]; then
  ok "deployed tools.json has 12 WHEN TO USE sentinels"
else
  bad "deployed tools.json: expected 12 sentinels, found '$SENT_COUNT' — rebuild image with annotated tools.json"
fi

# ---------- Pattern C ----------
step "Pattern C — ambient channels (stub mode)"
SNAP_ID="smoke-$(date +%s)-$$"
PAYLOAD_TG="{\"snapshot_id\":\"$SNAP_ID\",\"delivery_channel\":\"telegram\",\"recipient\":\"123\",\"png_s3_bucket\":\"test-bucket\",\"png_s3_key\":\"snapshots/smoke.png\",\"caption\":\"smoke\"}"
PAYLOAD_WA="{\"snapshot_id\":\"$SNAP_ID\",\"delivery_channel\":\"whatsapp\",\"recipient\":\"250788000000\",\"png_s3_bucket\":\"test-bucket\",\"png_s3_key\":\"snapshots/smoke.png\",\"caption\":\"smoke\"}"

docker exec "$REDIS_CONTAINER" redis-cli PUBLISH mundi:render_snapshot "$PAYLOAD_TG" >/dev/null
docker exec "$REDIS_CONTAINER" redis-cli PUBLISH mundi:render_snapshot "$PAYLOAD_WA" >/dev/null
sleep 2

if docker logs --since 30s "$TELEGRAM_CONTAINER" 2>&1 | grep -q "$SNAP_ID"; then
  ok "telegram-sender stub logged snap=$SNAP_ID"
else
  bad "telegram-sender stub did NOT log snap=$SNAP_ID"
fi

if docker logs --since 30s "$WHATSAPP_CONTAINER" 2>&1 | grep -q "$SNAP_ID"; then
  ok "whatsapp-sender stub logged snap=$SNAP_ID"
else
  bad "whatsapp-sender stub did NOT log snap=$SNAP_ID"
fi

# ---------- sender unit tests ----------
# Pure-Python tests for _handle() — runs inside the sender container (which has
# the senders module + pytest already installed) with --noconftest because the
# repo-wide conftest pulls in the full FastAPI app and postgres settings which
# aren't available in a stripped sender container.
step "sender unit tests (telegram + whatsapp)"
if [ -f "$REPO_ROOT/tests/test_telegram_sender.py" ] && [ -f "$REPO_ROOT/tests/test_whatsapp_sender.py" ]; then
  docker cp "$REPO_ROOT/tests/test_telegram_sender.py" \
    "$TELEGRAM_CONTAINER:/app/tests/test_telegram_sender.py" >/dev/null 2>&1
  docker cp "$REPO_ROOT/tests/test_whatsapp_sender.py" \
    "$TELEGRAM_CONTAINER:/app/tests/test_whatsapp_sender.py" >/dev/null 2>&1
  if docker exec -w /app "$TELEGRAM_CONTAINER" \
       pytest -q --noconftest tests/test_telegram_sender.py tests/test_whatsapp_sender.py \
       >/tmp/smoke_sender_tests.out 2>&1; then
    PASSCOUNT="$(grep -oE '[0-9]+ passed' /tmp/smoke_sender_tests.out | head -1)"
    ok "sender tests: ${PASSCOUNT:-passed}"
  else
    bad "sender tests failed (see /tmp/smoke_sender_tests.out)"
    tail -30 /tmp/smoke_sender_tests.out
  fi
else
  bad "sender test files missing under tests/"
fi

# ---------- cron + render_snapshot unit tests ----------
# Pure-Python tests for src/cron/sage_alerts.py + src/cron/cron_expr.py +
# src/tools/render_snapshot.py. Runs inside the app container so all deps
# (pydantic, redis.asyncio, pytest-asyncio) are available. Source + tests are
# docker-cp'd in because the currently-deployed image may pre-date these
# modules — passing tests against the latest source IS the deployment gate.
step "cron + render_snapshot unit tests"
TEST_FILES=(
  "$REPO_ROOT/tests/test_cron_expr.py"
  "$REPO_ROOT/tests/test_sage_alerts.py"
  "$REPO_ROOT/tests/test_render_snapshot.py"
)
SRC_OK=1
for f in "${TEST_FILES[@]}"; do [ -f "$f" ] || SRC_OK=0; done
if [ -d "$REPO_ROOT/src/cron" ] && [ -f "$REPO_ROOT/src/tools/render_snapshot.py" ] && [ "$SRC_OK" -eq 1 ]; then
  docker cp "$REPO_ROOT/src/cron" "$APP_CONTAINER:/app/src/cron" >/dev/null 2>&1
  docker cp "$REPO_ROOT/src/tools/render_snapshot.py" \
    "$APP_CONTAINER:/app/src/tools/render_snapshot.py" >/dev/null 2>&1
  for f in "${TEST_FILES[@]}"; do
    docker cp "$f" "$APP_CONTAINER:/app/tests/$(basename "$f")" >/dev/null 2>&1
  done
  if docker exec -w /app "$APP_CONTAINER" \
       pytest -q --noconftest \
         tests/test_cron_expr.py tests/test_sage_alerts.py tests/test_render_snapshot.py \
       >/tmp/smoke_cron_render_tests.out 2>&1; then
    PASSCOUNT="$(grep -oE '[0-9]+ passed' /tmp/smoke_cron_render_tests.out | head -1)"
    ok "cron+render tests: ${PASSCOUNT:-passed}"
  else
    bad "cron+render tests failed (see /tmp/smoke_cron_render_tests.out)"
    tail -30 /tmp/smoke_cron_render_tests.out
  fi
else
  bad "cron/render_snapshot source or test files missing"
fi

# ---------- write-side RLS WITH CHECK tests ----------
# Verifies migration c1d2e3f4a5bc closed the write-side gap on the 8
# tenant_isolation_* policies that c1d2e3f4a5b9 / c1d2e3f4a5bb shipped
# USING-only. Without WITH CHECK, a compromised partner session could INSERT
# rows attributed to another partner_uuid (RLS would just hide them from the
# attacker, not from the victim). 11 tests, each producing a real cross-
# partner write attempt — must all be rejected with InsufficientPrivilegeError
# (SQLSTATE 42501), except the admin-bypass pin which must succeed.
#
# Uses the full repo conftest (NOT --noconftest) because the test file imports
# src.database.pool._build_postgres_url to wire its own connections against
# the live database.
step "write-side RLS WITH CHECK tests"
RLS_TEST="$REPO_ROOT/tests/test_rls_with_check_writeside.py"
if [ -f "$RLS_TEST" ]; then
  docker cp "$RLS_TEST" "$APP_CONTAINER:/app/tests/test_rls_with_check_writeside.py" >/dev/null 2>&1
  if docker exec -w /app "$APP_CONTAINER" \
       pytest -q tests/test_rls_with_check_writeside.py \
       >/tmp/smoke_rls_wcheck.out 2>&1; then
    PASSCOUNT="$(grep -oE '[0-9]+ passed' /tmp/smoke_rls_wcheck.out | head -1)"
    ok "RLS WITH CHECK tests: ${PASSCOUNT:-passed}"
  else
    bad "RLS WITH CHECK tests failed (see /tmp/smoke_rls_wcheck.out)"
    tail -40 /tmp/smoke_rls_wcheck.out
  fi
else
  bad "tests/test_rls_with_check_writeside.py missing"
fi

# ---------- Pattern D ----------
step "Pattern D — runtime-bound composition"
if docker exec "$APP_CONTAINER" python -c "
import os
assert os.environ.get('INGABE_PATTERN_D_COMPOSITION', '1') == '1', 'D disabled by env'
t = float(os.environ.get('INGABE_PATTERN_D_TOOL_TIMEOUT_SEC', '60'))
assert t > 0, f'bad timeout {t}'
" >/dev/null 2>&1; then
  ok "INGABE_PATTERN_D_COMPOSITION defaults on; tool timeout >0"
else
  bad "Pattern D env preconditions failed"
fi

# ---------- summary ----------
step "summary"
printf '  pass=%d fail=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
