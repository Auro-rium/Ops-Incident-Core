#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AZURE_RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
NAME_PREFIX="${NAME_PREFIX:?NAME_PREFIX is required}"
MCP_APP_NAME="${MCP_APP_NAME:-${NAME_PREFIX}-mcp}"
PROJECT_ID="${PROJECT_ID:-${INCIDENTOPS_PROJECT_ID:-}}"
SEARCH_QUERY="${SEARCH_QUERY:-Where is the history service implemented?}"
INVESTIGATE_QUERY="${INVESTIGATE_QUERY:-Which parts of the Temporal repo are relevant to investigating workflow task latency?}"
MCP_URL="${MCP_URL:-http://127.0.0.1:8080/mcp}"

"$ROOT_DIR/scripts/azure_login_check.sh"

if [[ -z "$PROJECT_ID" ]]; then
  echo "PROJECT_ID or INCIDENTOPS_PROJECT_ID is required for readiness/search/investigation MCP tool smoke." >&2
  exit 1
fi

echo "Running MCP smoke from a short-lived Azure Container Apps job..."
MCP_JOB_NAME="${MCP_JOB_NAME:-${NAME_PREFIX}-benchmark-job}"
CORE_IMAGE="${CORE_IMAGE:-$(az containerapp show --resource-group "$AZURE_RESOURCE_GROUP" --name "${NAME_PREFIX}-core-api" --query 'properties.template.containers[0].image' --output tsv --only-show-errors)}"
MCP_TOKEN="${MCP_TOKEN:?MCP_TOKEN is required for the private MCP job smoke}"
az containerapp job start \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$MCP_JOB_NAME" \
  --image "$CORE_IMAGE" \
  --command python \
  --args scripts/mcp_probe.py \
  --env-vars \
    PROJECT_ID="$PROJECT_ID" \
    MCP_URL="$MCP_URL" \
    MCP_PROBE_QUERY="$SEARCH_QUERY" \
    MCP_TOKEN="$MCP_TOKEN" \
  --only-show-errors \
  --no-wait >/dev/null

echo "MCP probe job started; inspect Azure job logs for the safe tool summary."
