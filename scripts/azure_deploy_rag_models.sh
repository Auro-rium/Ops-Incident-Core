#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AZURE_RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
AZURE_ML_WORKSPACE="${AZURE_ML_WORKSPACE:?AZURE_ML_WORKSPACE is required}"
ACR_NAME="${ACR_NAME:?ACR_NAME is required}"
IMAGE_TAG="${IMAGE_TAG:?IMAGE_TAG is required}"
: "${BGE_M3_REVISION:?BGE_M3_REVISION must be an immutable bge-m3 revision}"
: "${BGE_RERANKER_REVISION:?BGE_RERANKER_REVISION must be an immutable reranker revision}"
RAG_EMBEDDING_ENDPOINT_NAME="${RAG_EMBEDDING_ENDPOINT_NAME:-incidentops-embed-gpu}"
RAG_RERANKER_ENDPOINT_NAME="${RAG_RERANKER_ENDPOINT_NAME:-incidentops-rerank-gpu}"

az extension add --name ml --yes --only-show-errors
acr_server="$(az acr show --name "$ACR_NAME" --query loginServer --output tsv --only-show-errors)"
temp_dir="$(mktemp -d)"
trap 'rm -rf "$temp_dir"' EXIT

for deployment in embedding reranker; do
  if [ "$deployment" = "embedding" ]; then
    endpoint_name="$RAG_EMBEDDING_ENDPOINT_NAME"
    model_revision="$BGE_M3_REVISION"
  else
    endpoint_name="$RAG_RERANKER_ENDPOINT_NAME"
    model_revision="$BGE_RERANKER_REVISION"
  fi
  sed -e "s|<ENDPOINT_NAME>|$endpoint_name|g" "$ROOT_DIR/infra/azure/rag-models/endpoint.yml" > "$temp_dir/${deployment}-endpoint.yml"
  az ml online-endpoint create --resource-group "$AZURE_RESOURCE_GROUP" --workspace-name "$AZURE_ML_WORKSPACE" --file "$temp_dir/${deployment}-endpoint.yml" --only-show-errors || \
    az ml online-endpoint show --resource-group "$AZURE_RESOURCE_GROUP" --workspace-name "$AZURE_ML_WORKSPACE" --name "$endpoint_name" --only-show-errors >/dev/null
  template="$ROOT_DIR/infra/azure/rag-models/${deployment}-deployment.yml"
  sed \
    -e "s|<ENDPOINT_NAME>|$endpoint_name|g" \
    -e "s|<ACR_LOGIN_SERVER>|$acr_server|g" \
    -e "s|<IMAGE_TAG>|$IMAGE_TAG|g" \
    -e "s|<MODEL_REVISION>|$model_revision|g" \
    "$template" > "$temp_dir/${deployment}.yml"
  az ml online-deployment create --resource-group "$AZURE_RESOURCE_GROUP" --workspace-name "$AZURE_ML_WORKSPACE" --file "$temp_dir/${deployment}.yml" --all-traffic --only-show-errors
done

printf 'RAG_EMBEDDING_ENDPOINT='
az ml online-endpoint show --resource-group "$AZURE_RESOURCE_GROUP" --workspace-name "$AZURE_ML_WORKSPACE" --name "$RAG_EMBEDDING_ENDPOINT_NAME" --query scoring_uri --output tsv --only-show-errors
printf 'RAG_RERANKER_ENDPOINT='
az ml online-endpoint show --resource-group "$AZURE_RESOURCE_GROUP" --workspace-name "$AZURE_ML_WORKSPACE" --name "$RAG_RERANKER_ENDPOINT_NAME" --query scoring_uri --output tsv --only-show-errors
