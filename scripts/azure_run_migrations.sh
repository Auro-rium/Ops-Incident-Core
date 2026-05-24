#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

AZURE_RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-incidentops-demo-swc-rg}"
MIGRATION_JOB_NAME="${MIGRATION_JOB_NAME:-incidentops-core-migrate}"
CORE_API_APP_NAME="${CORE_API_APP_NAME:-incidentops-core-api}"
CORE_WORKER_APP_NAME="${CORE_WORKER_APP_NAME:-incidentops-core-worker}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"

"$ROOT_DIR/scripts/azure_login_check.sh"

echo "Starting migration job '$MIGRATION_JOB_NAME'..."
execution_name="$(az containerapp job start \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$MIGRATION_JOB_NAME" \
  --query name \
  --output tsv \
  --only-show-errors)"

deadline=$((SECONDS + TIMEOUT_SECONDS))
status=""
while (( SECONDS < deadline )); do
  status="$(az containerapp job execution list \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$MIGRATION_JOB_NAME" \
    --query "[?name=='$execution_name'].properties.status | [0]" \
    --output tsv \
    --only-show-errors 2>/dev/null || true)"
  case "$status" in
    Succeeded)
      echo "Migration job succeeded: $execution_name"
      restart_value="$(date -u +%Y%m%d%H%M%S)"
      echo "Restarting Core API and worker revisions after migration..."
      az containerapp update \
        --resource-group "$AZURE_RESOURCE_GROUP" \
        --name "$CORE_API_APP_NAME" \
        --set-env-vars "RESTART_AFTER_MIGRATION=$restart_value" \
        --only-show-errors >/dev/null
      az containerapp update \
        --resource-group "$AZURE_RESOURCE_GROUP" \
        --name "$CORE_WORKER_APP_NAME" \
        --set-env-vars "RESTART_AFTER_MIGRATION=$restart_value" \
        --only-show-errors >/dev/null
      exit 0
      ;;
    Failed)
      echo "Migration job failed: $execution_name" >&2
      az containerapp job logs show \
        --resource-group "$AZURE_RESOURCE_GROUP" \
        --name "$MIGRATION_JOB_NAME" \
        --execution "$execution_name" \
        --tail 200 || true
      exit 1
      ;;
  esac
  sleep 5
done

echo "Timed out waiting for migration job '$execution_name' (last status: ${status:-unknown})" >&2
exit 1
