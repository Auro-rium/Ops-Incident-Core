#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

AZURE_RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
AZURE_LOCATION="${AZURE_LOCATION:?AZURE_LOCATION is required}"
NAME_PREFIX="${NAME_PREFIX:?NAME_PREFIX is required}"
ENVIRONMENT_NAME="${ENVIRONMENT_NAME:?ENVIRONMENT_NAME is required}"
ACR_NAME="${ACR_NAME:?ACR_NAME is required}"
IMAGE_TAG="${IMAGE_TAG:-$(git -C "$ROOT_DIR" rev-parse --short HEAD)}"
DEPLOY_FRONTEND="${DEPLOY_FRONTEND:-true}"
CORS_ORIGINS="${CORS_ORIGINS:?CORS_ORIGINS is required}"
BOOTSTRAP_ADMIN_EMAIL="${BOOTSTRAP_ADMIN_EMAIL:-admin@incidentops.local}"
POSTGRES_ADMIN_USER="${POSTGRES_ADMIN_USER:-incidentops}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -hex 24 | tr -d '\n')}"
JWT_SECRET="${JWT_SECRET:-$(openssl rand -base64 48 | tr -d '\n')}"
BOOTSTRAP_ADMIN_PASSWORD="${BOOTSTRAP_ADMIN_PASSWORD:-$(openssl rand -hex 20 | tr -d '\n')}"
INCIDENTOPS_TOKEN="${INCIDENTOPS_TOKEN:-}"
INCIDENTOPS_PROJECT_ID="${INCIDENTOPS_PROJECT_ID:-}"
COLLECTOR_REPO_URL="${COLLECTOR_REPO_URL:-}"
INCIDENTOPS_MCP_TOKEN="${INCIDENTOPS_MCP_TOKEN:-}"
AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-}"
AZURE_OPENAI_API_KEY="${AZURE_OPENAI_API_KEY:-}"
AZURE_OPENAI_API_VERSION="${AZURE_OPENAI_API_VERSION:-2024-10-21}"
AZURE_OPENAI_CHAT_DEPLOYMENT="${AZURE_OPENAI_CHAT_DEPLOYMENT:-}"
AZURE_OPENAI_EMBEDDING_DEPLOYMENT="${AZURE_OPENAI_EMBEDDING_DEPLOYMENT:-}"
QDRANT_URL="${QDRANT_URL:-}"
QDRANT_API_KEY="${QDRANT_API_KEY:-}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-incidentops_chunks}"
EMBEDDING_DIMENSION="${EMBEDDING_DIMENSION:-1024}"
OUTPUT_FILE="${OUTPUT_FILE:-$ROOT_DIR/infra/azure/.last-deployment.json}"
PARAMETERS_FILE="$(mktemp)"
DEPLOY_ERROR_FILE="$(mktemp)"
trap 'rm -f "$PARAMETERS_FILE" "$DEPLOY_ERROR_FILE"' EXIT

"$ROOT_DIR/scripts/azure_login_check.sh"

test -n "$AZURE_OPENAI_ENDPOINT" || { echo "AZURE_OPENAI_ENDPOINT is required." >&2; exit 1; }
test -n "$AZURE_OPENAI_API_KEY" || { echo "AZURE_OPENAI_API_KEY is required." >&2; exit 1; }
test -n "$AZURE_OPENAI_CHAT_DEPLOYMENT" || { echo "AZURE_OPENAI_CHAT_DEPLOYMENT is required." >&2; exit 1; }
test -n "$AZURE_OPENAI_EMBEDDING_DEPLOYMENT" || { echo "AZURE_OPENAI_EMBEDDING_DEPLOYMENT is required." >&2; exit 1; }
test -n "$QDRANT_URL" || { echo "QDRANT_URL is required." >&2; exit 1; }
test -n "$QDRANT_API_KEY" || { echo "QDRANT_API_KEY is required." >&2; exit 1; }

az group create \
  --name "$AZURE_RESOURCE_GROUP" \
  --location "$AZURE_LOCATION" \
  --only-show-errors >/dev/null

chmod 600 "$PARAMETERS_FILE"
export AZURE_LOCATION NAME_PREFIX ENVIRONMENT_NAME ACR_NAME IMAGE_TAG
export DEPLOY_FRONTEND
export POSTGRES_ADMIN_USER POSTGRES_PASSWORD JWT_SECRET BOOTSTRAP_ADMIN_EMAIL BOOTSTRAP_ADMIN_PASSWORD
export INCIDENTOPS_TOKEN INCIDENTOPS_PROJECT_ID COLLECTOR_REPO_URL INCIDENTOPS_MCP_TOKEN CORS_ORIGINS
export AZURE_OPENAI_ENDPOINT AZURE_OPENAI_API_KEY AZURE_OPENAI_API_VERSION
export AZURE_OPENAI_CHAT_DEPLOYMENT AZURE_OPENAI_EMBEDDING_DEPLOYMENT
export QDRANT_URL QDRANT_API_KEY QDRANT_COLLECTION EMBEDDING_DIMENSION
python3 - "$PARAMETERS_FILE" <<'PY'
import json
import os
import sys

param_names = {
    "location": "AZURE_LOCATION",
    "namePrefix": "NAME_PREFIX",
    "environmentName": "ENVIRONMENT_NAME",
    "acrName": "ACR_NAME",
    "coreImageTag": "IMAGE_TAG",
    "collectorImageTag": "IMAGE_TAG",
    "frontendImageTag": "IMAGE_TAG",
    "deployFrontend": "DEPLOY_FRONTEND",
    "postgresAdminUser": "POSTGRES_ADMIN_USER",
    "postgresAdminPassword": "POSTGRES_PASSWORD",
    "jwtSecret": "JWT_SECRET",
    "bootstrapAdminEmail": "BOOTSTRAP_ADMIN_EMAIL",
    "bootstrapAdminPassword": "BOOTSTRAP_ADMIN_PASSWORD",
    "incidentopsToken": "INCIDENTOPS_TOKEN",
    "incidentopsProjectId": "INCIDENTOPS_PROJECT_ID",
    "collectorRepoUrl": "COLLECTOR_REPO_URL",
    "incidentopsMcpToken": "INCIDENTOPS_MCP_TOKEN",
    "corsOrigins": "CORS_ORIGINS",
    "azureOpenAIEndpoint": "AZURE_OPENAI_ENDPOINT",
    "azureOpenAIApiKey": "AZURE_OPENAI_API_KEY",
    "azureOpenAIApiVersion": "AZURE_OPENAI_API_VERSION",
    "azureOpenAIChatDeployment": "AZURE_OPENAI_CHAT_DEPLOYMENT",
    "azureOpenAIEmbeddingDeployment": "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
    "qdrantUrl": "QDRANT_URL",
    "qdrantApiKey": "QDRANT_API_KEY",
    "qdrantCollection": "QDRANT_COLLECTION",
    "embeddingDimension": "EMBEDDING_DIMENSION",
}

def coerce_value(env_name: str):
    value = os.environ.get(env_name, "")
    if env_name == "DEPLOY_FRONTEND":
        return value.lower() == "true"
    if env_name == "EMBEDDING_DIMENSION":
        return int(value)
    return value

payload = {
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {
        bicep_name: {"value": coerce_value(env_name)}
        for bicep_name, env_name in param_names.items()
    },
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
PY

echo "Deploying Azure resources into resource group '$AZURE_RESOURCE_GROUP'..."
deployment_attempt=1
deployment_max_attempts="${AZURE_DEPLOY_MAX_ATTEMPTS:-3}"
while true; do
  if az deployment group create \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --template-file "$ROOT_DIR/infra/azure/main.bicep" \
    --parameters "@$PARAMETERS_FILE" \
    --output json > "$OUTPUT_FILE" 2> "$DEPLOY_ERROR_FILE"; then
    break
  fi

  if (( deployment_attempt >= deployment_max_attempts )) || ! grep -Eq 'ServerIsBusy|AnotherOperationInProgress|Retry later|Try again later' "$DEPLOY_ERROR_FILE"; then
    cat "$DEPLOY_ERROR_FILE" >&2
    exit 1
  fi

  sleep_seconds=$((30 * deployment_attempt))
  echo "Azure deployment attempt $deployment_attempt failed with a retryable busy-state error. Retrying in ${sleep_seconds}s..." >&2
  deployment_attempt=$((deployment_attempt + 1))
  sleep "$sleep_seconds"
done

chmod 600 "$OUTPUT_FILE"

echo "Deployment outputs:"
az deployment group show \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$(python3 - "$OUTPUT_FILE" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["name"])
PY
)" \
  --query 'properties.outputs' \
  --output table

echo "Saved deployment output metadata to $OUTPUT_FILE"
