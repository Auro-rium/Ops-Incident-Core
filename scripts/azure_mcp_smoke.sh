#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AZURE_RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
NAME_PREFIX="${NAME_PREFIX:?NAME_PREFIX is required}"
MCP_APP_NAME="${MCP_APP_NAME:-${NAME_PREFIX}-mcp}"
PROJECT_ID="${PROJECT_ID:-${INCIDENTOPS_PROJECT_ID:-}}"
SEARCH_QUERY="${SEARCH_QUERY:-Where is the history service implemented?}"
INVESTIGATE_QUERY="${INVESTIGATE_QUERY:-Which parts of the Temporal repo are relevant to investigating workflow task latency?}"
MCP_PROBE_TIMEOUT_SECONDS="${MCP_PROBE_TIMEOUT_SECONDS:-45}"
MCP_WAIT_TIMEOUT_SECONDS="${MCP_WAIT_TIMEOUT_SECONDS:-120}"
MCP_POLL_SECONDS="${MCP_POLL_SECONDS:-5}"

"$ROOT_DIR/scripts/azure_login_check.sh"

if [[ -z "$PROJECT_ID" ]]; then
  echo "PROJECT_ID or INCIDENTOPS_PROJECT_ID is required for readiness/search/investigation MCP tool smoke." >&2
  exit 1
fi

echo "Running MCP smoke from a short-lived Azure Container Apps job..."
MCP_JOB_NAME="${MCP_JOB_NAME:-${NAME_PREFIX}-benchmark-job}"
CORE_IMAGE="${CORE_IMAGE:-$(az containerapp show --resource-group "$AZURE_RESOURCE_GROUP" --name "${NAME_PREFIX}-core-api" --query 'properties.template.containers[0].image' --output tsv --only-show-errors)}"
MCP_TOKEN="${MCP_TOKEN:?MCP_TOKEN is required for the private MCP job smoke}"
MCP_FQDN="$(az containerapp show --resource-group "$AZURE_RESOURCE_GROUP" --name "$MCP_APP_NAME" --query 'properties.configuration.ingress.fqdn' --output tsv --only-show-errors)"
MCP_URL="${MCP_URL:-https://${MCP_FQDN}/mcp}"
if [[ -z "$MCP_FQDN" ]]; then
  echo "Unable to resolve the private MCP app FQDN for $MCP_APP_NAME." >&2
  exit 1
fi
echo "MCP target: private app $MCP_APP_NAME (URL host only: ${MCP_FQDN})"
az containerapp job secret set \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$MCP_JOB_NAME" \
  --secrets "incidentops-mcp-token=$MCP_TOKEN" \
  --only-show-errors >/dev/null
execution_name="$(az containerapp job start \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$MCP_JOB_NAME" \
  --image "$CORE_IMAGE" \
  --command python \
  --args scripts/mcp_probe.py \
  --env-vars \
    PROJECT_ID="$PROJECT_ID" \
    MCP_URL="$MCP_URL" \
    MCP_PROBE_QUERY="$SEARCH_QUERY" \
    MCP_TOKEN=secretref:incidentops-mcp-token \
    MCP_PROBE_TIMEOUT_SECONDS="$MCP_PROBE_TIMEOUT_SECONDS" \
  --only-show-errors \
  --query name --output tsv --only-show-errors)"

if [[ -z "$execution_name" ]]; then
  echo "Azure did not return an MCP probe execution name." >&2
  exit 1
fi

deadline=$((SECONDS + MCP_WAIT_TIMEOUT_SECONDS))
status="Unknown"
while (( SECONDS < deadline )); do
  status="$(az containerapp job execution show \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --job-name "$MCP_JOB_NAME" \
    --job-execution-name "$execution_name" \
    --query 'properties.status' --output tsv --only-show-errors 2>/dev/null || true)"
  echo "mcp_probe_status: ${status:-Pending}"
  case "$status" in
    Succeeded|Completed)
      echo "MCP probe completed. Safe summary:"
      az containerapp job logs show --resource-group "$AZURE_RESOURCE_GROUP" --name "$MCP_JOB_NAME" --execution "$execution_name" --container benchmark --tail 120 --only-show-errors || true
      exit 0
      ;;
    Failed|Canceled|Cancelled)
      echo "MCP probe failed. Safe logs:" >&2
      az containerapp job logs show --resource-group "$AZURE_RESOURCE_GROUP" --name "$MCP_JOB_NAME" --execution "$execution_name" --container benchmark --tail 120 --only-show-errors || true
      exit 1
      ;;
  esac
  sleep "$MCP_POLL_SECONDS"
done

echo "MCP probe timed out after ${MCP_WAIT_TIMEOUT_SECONDS}s. Safe logs:" >&2
az containerapp job logs show --resource-group "$AZURE_RESOURCE_GROUP" --name "$MCP_JOB_NAME" --execution "$execution_name" --container benchmark --tail 120 --only-show-errors || true
exit 1
