#!/usr/bin/env bash
# Reproducible smoke for Patterns A+B+D against a running docker stack.
#
# Pattern A — active-memory injection: INGABE_PATTERN_A_ACTIVE_MEMORY default on,
#             brain_service importable.
# Pattern B — tool-description disambiguation: 12 target tools in the deployed
#             tools.json carry the "WHEN TO USE:" sentinel.
# Pattern D — runtime-bound composition: INGABE_PATTERN_D_COMPOSITION default on,
#             INGABE_PATTERN_D_TOOL_TIMEOUT_SEC default 60.
#
# The app container name is configurable via env so this runs unchanged in
# CI: APP_CONTAINER. (Pattern C, the WhatsApp/Telegram channels, was removed
# along with the channels.)

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

APP_CONTAINER="${APP_CONTAINER:-mundi-app}"

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
ok "app running"

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
# The sentinels live in the committed tools.json; we verify the count inside
# the running container as the deployment gate.
step "Pattern B — tool-description disambiguation"

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
