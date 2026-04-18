#!/usr/bin/env bash
# ------------------------------------------------------------------
# Deploy mundi.ai to Hetzner prod (gis.nozalabs.rw).
#
# Model: rsync source tree to the deploy user, rebuild the app image
# on the server, restart the app container, verify the Brain ingestion
# scheduler came up healthy. No git on the server — the local checkout
# is authoritative.
#
# Usage:
#   scripts/deploy.sh                 # interactive, prompts before restart
#   scripts/deploy.sh -y              # non-interactive
#   scripts/deploy.sh --dry-run       # rsync --dry-run, no remote changes
#   scripts/deploy.sh --no-build      # skip image rebuild (content-only)
#   DEPLOY_HOST=1.2.3.4 scripts/deploy.sh   # override target
# ------------------------------------------------------------------
set -euo pipefail

DEPLOY_HOST="${DEPLOY_HOST:-178.104.18.44}"
DEPLOY_USER="${DEPLOY_USER:-deploy}"
DEPLOY_PATH="${DEPLOY_PATH:-/home/deploy/mundi.ai}"
# rsync goes in as root because deploy's SSH key isn't on the local box;
# root chowns to deploy after sync. If deploy SSH is set up, set RSYNC_USER=deploy.
RSYNC_USER="${RSYNC_USER:-root}"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file .env.prod"

DRY_RUN=0
NO_BUILD=0
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)  DRY_RUN=1; shift ;;
    --no-build) NO_BUILD=1; shift ;;
    -y|--yes)   ASSUME_YES=1; shift ;;
    -h|--help)
      sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

here() { cd "$(dirname "$0")/.."; pwd; }
REPO_ROOT="$(here)"
cd "$REPO_ROOT"

log() { printf '\033[1;36m[deploy]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[deploy]\033[0m %s\n' "$*" >&2; }

# --- Sanity checks -------------------------------------------------
[[ -f .env.example ]] || { err "run from a checkout with .env.example"; exit 1; }
[[ -f docker-compose.prod.yml ]] || { err "no docker-compose.prod.yml"; exit 1; }

if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "${RSYNC_USER}@${DEPLOY_HOST}" 'true' 2>/dev/null; then
  err "SSH to ${RSYNC_USER}@${DEPLOY_HOST} failed (BatchMode, no prompt)"
  exit 1
fi

GIT_REV="$(git rev-parse --short HEAD 2>/dev/null || echo 'nogit')"
GIT_BRANCH="$(git branch --show-current 2>/dev/null || echo 'detached')"
DIRTY=""
if git diff --quiet 2>/dev/null && git diff --cached --quiet 2>/dev/null; then
  :
else
  DIRTY=" (dirty)"
fi

log "target:   ${DEPLOY_USER}@${DEPLOY_HOST}:${DEPLOY_PATH}"
log "local:    ${GIT_BRANCH} @ ${GIT_REV}${DIRTY}"
log "compose:  ${COMPOSE}"
[[ $DRY_RUN -eq 1 ]] && log "mode:     DRY-RUN (no remote changes)"
[[ $NO_BUILD -eq 1 ]] && log "mode:     no-build (restart only)"

if [[ $ASSUME_YES -ne 1 && $DRY_RUN -ne 1 ]]; then
  read -r -p "proceed? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { log "aborted"; exit 0; }
fi

# --- 1. rsync tree -------------------------------------------------
RSYNC_EXCLUDES=(
  --exclude='.git/' --exclude='.venv/' --exclude='node_modules/'
  --exclude='__pycache__/' --exclude='*.pyc' --exclude='.pytest_cache/'
  --exclude='.mypy_cache/' --exclude='.ruff_cache/'
  --exclude='.env' --exclude='.env.local' --exclude='.env.prod'
  --exclude='frontendts/dist/' --exclude='frontendts/.vite/'
  --exclude='data/' --exclude='working/' --exclude='noza-web/'
  --exclude='.claude/' --exclude='.gstack/' --exclude='.DS_Store'
  --exclude='test_fixtures/large/'
)
RSYNC_FLAGS=(-az --delete --stats)
[[ $DRY_RUN -eq 1 ]] && RSYNC_FLAGS+=(--dry-run -v)

log "rsync →  ${RSYNC_USER}@${DEPLOY_HOST}:${DEPLOY_PATH}"
rsync "${RSYNC_FLAGS[@]}" "${RSYNC_EXCLUDES[@]}" \
  ./ "${RSYNC_USER}@${DEPLOY_HOST}:${DEPLOY_PATH}/"

if [[ $DRY_RUN -eq 1 ]]; then
  log "dry-run complete, stopping"
  exit 0
fi

# Ensure deploy owns everything we just wrote as root.
if [[ "$RSYNC_USER" == "root" ]]; then
  log "chown → ${DEPLOY_USER}:${DEPLOY_USER}"
  ssh "${RSYNC_USER}@${DEPLOY_HOST}" \
    "chown -R ${DEPLOY_USER}:${DEPLOY_USER} ${DEPLOY_PATH}"
fi

# --- 2. rebuild + restart -----------------------------------------
SSH_DEPLOY() {
  ssh "${RSYNC_USER}@${DEPLOY_HOST}" \
    "sudo -u ${DEPLOY_USER} -i bash -c 'cd ${DEPLOY_PATH} && $*'"
}

if [[ $NO_BUILD -eq 0 ]]; then
  log "rebuild app image on server (CPX42, ~2-4 min)"
  SSH_DEPLOY "${COMPOSE} build app"
fi

log "restart app"
SSH_DEPLOY "${COMPOSE} up -d app"

# --- 3. verify -----------------------------------------------------
# Bounded poll instead of fixed sleep. Old behaviour slept 8s then grepped
# --tail=200 of the container log, which both (a) falsely failed on cold
# images where lifespan takes >8s, and (b) could match the *previous*
# container's startup line if docker reused the name. The --since window
# pins us to logs from this deploy; the loop gives slow boots a real
# chance to finish.
DEPLOY_START_TS="$(SSH_DEPLOY "date -u +'%Y-%m-%dT%H:%M:%S'" || date -u +'%Y-%m-%dT%H:%M:%S')"
BOOT_TIMEOUT=120  # seconds
log "waiting up to ${BOOT_TIMEOUT}s for app to boot (since ${DEPLOY_START_TS})"

deadline=$((SECONDS + BOOT_TIMEOUT))
STATUS=""
while (( SECONDS < deadline )); do
  STATUS="$(SSH_DEPLOY "${COMPOSE} ps app --format '{{.Status}}'" || true)"
  [[ "$STATUS" =~ ^Up ]] && break
  sleep 3
done
log "container: ${STATUS}"
if ! [[ "$STATUS" =~ ^Up ]]; then
  err "app did not reach Up state within ${BOOT_TIMEOUT}s"
  SSH_DEPLOY "docker logs mundi-app --tail=60" || true
  exit 1
fi

log "checking ingestion scheduler startup log"
scheduler_ok=0
while (( SECONDS < deadline )); do
  if SSH_DEPLOY "docker logs mundi-app --since ${DEPLOY_START_TS} 2>&1 | grep -q ingestion_scheduler_started"; then
    scheduler_ok=1
    break
  fi
  sleep 3
done
if (( scheduler_ok == 1 )); then
  log "scheduler: ingestion_scheduler_started ✓"
else
  err "scheduler did not log ingestion_scheduler_started within ${BOOT_TIMEOUT}s — investigate:"
  SSH_DEPLOY "docker logs mundi-app --tail=80" || true
  exit 1
fi

log "deploy complete → ${GIT_BRANCH}@${GIT_REV}"
