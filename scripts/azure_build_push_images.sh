#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

AZURE_RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-incidentops-demo-swc-rg}"
AZURE_LOCATION="${AZURE_LOCATION:-swedencentral}"
ACR_NAME="${ACR_NAME:?ACR_NAME is required}"
IMAGE_TAG="${IMAGE_TAG:-$(git -C "$ROOT_DIR" rev-parse --short HEAD)}"
CORE_REPO_PATH="${CORE_REPO_PATH:-$ROOT_DIR}"
BUILD_FRONTEND="${BUILD_FRONTEND:-false}"

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

FRONTEND_REPO_PATH=""
if [[ "$BUILD_FRONTEND" == "true" ]]; then
  FRONTEND_REPO_PATH="$(detect_repo "${FRONTEND_REPO_PATH:-}" \
    "$ROOT_DIR/../Ops-Incident-frontend" \
    "$ROOT_DIR/../incidentops-frontend")" || {
    echo "Frontend repo not found. Set FRONTEND_REPO_PATH." >&2
    exit 1
  }
fi

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

login_server="$(az acr show \
  --name "$ACR_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --query loginServer \
  --output tsv \
  --only-show-errors)"

echo "Logging Docker into ACR '$ACR_NAME'..."
az acr login --name "$ACR_NAME" --only-show-errors >/dev/null

echo "Building and pushing images to ACR '$ACR_NAME' with tag '$IMAGE_TAG'..."
docker build -t "$login_server/incidentops-core:$IMAGE_TAG" "$CORE_REPO_PATH"
docker push "$login_server/incidentops-core:$IMAGE_TAG"
docker build -t "$login_server/opsincident-collector:$IMAGE_TAG" "$COLLECTOR_REPO_PATH"
docker push "$login_server/opsincident-collector:$IMAGE_TAG"
if [[ "$BUILD_FRONTEND" == "true" ]]; then
  docker build -t "$login_server/incidentops-frontend:$IMAGE_TAG" "$FRONTEND_REPO_PATH"
  docker push "$login_server/incidentops-frontend:$IMAGE_TAG"
fi

cat <<EOF
Images pushed:
  $login_server/incidentops-core:$IMAGE_TAG
  $login_server/opsincident-collector:$IMAGE_TAG
$(if [[ "$BUILD_FRONTEND" == "true" ]]; then printf '  %s/incidentops-frontend:%s\n' "$login_server" "$IMAGE_TAG"; else printf '  frontend build skipped\n'; fi)

Use IMAGE_TAG=$IMAGE_TAG for scripts/azure_deploy.sh.
EOF
