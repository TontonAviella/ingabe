#!/bin/bash
# Grafana's MCP server for Claude Code, run on the live server and spoken to over ssh (stdio), so Claude Code can
# read the dashboards, Prometheus and Loki (logs) where problems arise. Read-only: a Viewer service-account token
# (GRAFANA_MCP_TOKEN in .env) and write tools turned off. The token stays on the server: it is passed through the
# environment, not the command line.
#   Claude Code (on the Mac): claude mcp add grafana -- ssh -i ~/.ssh/ingabe_contabo -o BatchMode=yes \
#     ingabe@169.58.99.111 ~/ingabe/scripts/grafana_mcp.sh
set -euo pipefail
cd "$(dirname "$0")/.."
GRAFANA_SERVICE_ACCOUNT_TOKEN="$(grep '^GRAFANA_MCP_TOKEN=' .env | cut -d= -f2-)"
export GRAFANA_SERVICE_ACCOUNT_TOKEN
[ -n "$GRAFANA_SERVICE_ACCOUNT_TOKEN" ] || { echo "GRAFANA_MCP_TOKEN is not set in .env" >&2; exit 1; }
NETWORK="$(docker inspect mundi-grafana -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}')"
exec docker run -i --rm --network "$NETWORK" -e GRAFANA_URL=http://grafana:3000 -e GRAFANA_SERVICE_ACCOUNT_TOKEN \
  grafana/mcp-grafana:2.0.2 -t stdio -disable-write -disable-admin -log-level error
