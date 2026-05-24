#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

AZURE_RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-incidentops-demo-rg}"
AZURE_LOCATION="${AZURE_LOCATION:-eastus}"
ACR_NAME="${ACR_NAME:?ACR_NAME is required}"
IMAGE_TAG="${IMAGE_TAG:-$(git -C "$ROOT_DIR" rev-parse --short HEAD)}"
CORE_REPO_PATH="${CORE_REPO_PATH:-$ROOT_DIR}"

detect_repo() {
  local env_value="$1"
  shift
  if [[ -n "$env_value" && -d "$env_value" ]]; then
    printf '%s\n' "$env_value"
    return 0
  fi
  local candidate
  for candidate in "$@"; do
    if [[ -d "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

COLLECTOR_REPO_PATH="$(detect_repo "${COLLECTOR_REPO_PATH:-}" \
  "$ROOT_DIR/../Ops-Incident-Collector" \
  "$ROOT_DIR/../OpsIncident-Collector")" || {
  echo "Collector repo not found. Set COLLECTOR_REPO_PATH." >&2
  exit 1
}

FRONTEND_REPO_PATH="$(detect_repo "${FRONTEND_REPO_PATH:-}" \
  "$ROOT_DIR/../Ops-Incident-frontend" \
  "$ROOT_DIR/../incidentops-frontend")" || {
  echo "Frontend repo not found. Set FRONTEND_REPO_PATH." >&2
  exit 1
}

"$ROOT_DIR/scripts/azure_login_check.sh"

az group create \
  --name "$AZURE_RESOURCE_GROUP" \
  --location "$AZURE_LOCATION" \
  --only-show-errors >/dev/null

if ! az acr show --name "$ACR_NAME" --resource-group "$AZURE_RESOURCE_GROUP" --only-show-errors >/dev/null 2>&1; then
  az acr create \
    --name "$ACR_NAME" \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --location "$AZURE_LOCATION" \
    --sku Basic \
    --admin-enabled false \
    --only-show-errors >/dev/null
fi

echo "Building and pushing images to ACR '$ACR_NAME' with tag '$IMAGE_TAG'..."
az acr build --registry "$ACR_NAME" --image "incidentops-core:$IMAGE_TAG" "$CORE_REPO_PATH" --only-show-errors
az acr build --registry "$ACR_NAME" --image "opsincident-collector:$IMAGE_TAG" "$COLLECTOR_REPO_PATH" --only-show-errors
az acr build --registry "$ACR_NAME" --image "incidentops-frontend:$IMAGE_TAG" "$FRONTEND_REPO_PATH" --only-show-errors

cat <<EOF
Images pushed:
  $ACR_NAME.azurecr.io/incidentops-core:$IMAGE_TAG
  $ACR_NAME.azurecr.io/opsincident-collector:$IMAGE_TAG
  $ACR_NAME.azurecr.io/incidentops-frontend:$IMAGE_TAG

Use IMAGE_TAG=$IMAGE_TAG for scripts/azure_deploy.sh.
EOF
