#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

docker inspect mundi-app >/dev/null 2>&1 || {
  echo "mundi-app is not running; start it with scripts/deploy.sh" >&2
  exit 1
}

exec docker exec mundi-app python -m src.services.runtime_audit "$@"
