#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

AZURE_RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-incidentops-demo-swc-rg}"
BOOTSTRAP_JOB_NAME="${BOOTSTRAP_JOB_NAME:-incidentops-bootstrap-admin}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-600}"

"$ROOT_DIR/scripts/azure_login_check.sh"

echo "Starting bootstrap-admin job '$BOOTSTRAP_JOB_NAME'..."
execution_name="$(az containerapp job start \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$BOOTSTRAP_JOB_NAME" \
  --query name \
  --output tsv \
  --only-show-errors)"

deadline=$((SECONDS + TIMEOUT_SECONDS))
status=""
while (( SECONDS < deadline )); do
  status="$(az containerapp job execution list \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$BOOTSTRAP_JOB_NAME" \
    --query "[?name=='$execution_name'].properties.status | [0]" \
    --output tsv \
    --only-show-errors 2>/dev/null || true)"
  case "$status" in
    Succeeded)
      echo "Bootstrap-admin job succeeded: $execution_name"
      exit 0
      ;;
    Failed)
      echo "Bootstrap-admin job failed: $execution_name" >&2
      az containerapp job logs show \
        --resource-group "$AZURE_RESOURCE_GROUP" \
        --name "$BOOTSTRAP_JOB_NAME" \
        --execution "$execution_name" \
        --tail 200 || true
      exit 1
      ;;
  esac
  sleep 5
done

echo "Timed out waiting for bootstrap-admin job '$execution_name' (last status: ${status:-unknown})" >&2
exit 1
