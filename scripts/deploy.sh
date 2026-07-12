#!/usr/bin/env bash
# Build, start, and verify Ingabe on this machine only.
set -euo pipefail

BUILD=1
CHECK_ONLY=0
TIMEOUT_SECONDS=180

usage() {
  cat <<'EOF'
Usage: scripts/deploy.sh [--no-build] [--check-only] [--timeout SECONDS]

This command is intentionally local-only. It never uses SSH, rsync, a remote
host, or a production compose override. The built application is available at
http://localhost:8000 after the checks pass.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-build)
      BUILD=0
      shift
      ;;
    --check-only)
      CHECK_ONLY=1
      BUILD=0
      shift
      ;;
    --timeout)
      [[ $# -ge 2 ]] || { echo "--timeout requires seconds" >&2; exit 2; }
      TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ "$TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] && (( TIMEOUT_SECONDS > 0 )) || {
  echo "--timeout must be a positive integer" >&2
  exit 2
}

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

log() { printf '\033[1;36m[local-deploy]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[local-deploy]\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || fail "Docker is not installed"
docker info >/dev/null 2>&1 || fail "Docker Desktop is not running"
[[ -f docker-compose.yml ]] || fail "docker-compose.yml is missing"
[[ -s FastSAM-s.pt ]] || fail "FastSAM-s.pt is missing or empty"

if (( CHECK_ONLY == 0 )); then
  if (( BUILD == 1 )); then
    log "building the local app image"
    docker compose build app
  fi
  log "starting the local app and Dagster daemon"
  docker compose up -d app dagster-daemon
fi

deadline=$((SECONDS + TIMEOUT_SECONDS))
health_json=""
log "waiting for http://127.0.0.1:8000/health"
while (( SECONDS < deadline )); do
  health_json="$(curl --silent --show-error --fail --max-time 3 \
    http://127.0.0.1:8000/health 2>/dev/null || true)"
  if [[ -n "$health_json" ]] && HEALTH_JSON="$health_json" python3 - <<'PY'
import json
import os
import sys

payload = json.loads(os.environ["HEALTH_JSON"])
checks = payload.get("checks") or {}
required = ("postgres", "redis", "qgis")
sys.exit(0 if payload.get("status") == "healthy" and all(checks.get(k) == "ok" for k in required) else 1)
PY
  then
    break
  fi
  sleep 2
done
[[ -n "$health_json" ]] || fail "local API did not return health data"

for container in mundi-app mundi-dagster-daemon; do
  state="$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || true)"
  [[ "$state" == "running" ]] || fail "$container is not running (state: ${state:-missing})"
done

docker exec mundi-app test -s /app/FastSAM-s.pt \
  || fail "FastSAM weights are not mounted in the app container"
curl --silent --show-error --fail --max-time 5 http://127.0.0.1:8000/ \
  | grep -qi '<!doctype html' \
  || fail "the local frontend did not return its HTML shell"
curl --silent --show-error --fail --max-time 5 \
  http://127.0.0.1:9000/minio/health/live >/dev/null \
  || fail "local MinIO is not healthy"
docker exec mundi-app python -m src.services.runtime_audit >/dev/null \
  || fail "local runtime capability audit failed"

log "API, frontend, FastSAM, Dagster, Postgres, Redis, QGIS, and MinIO are healthy"
log "local app: http://localhost:8000"
