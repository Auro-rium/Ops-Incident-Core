#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACR_NAME="${ACR_NAME:?ACR_NAME is required}"
IMAGE_TAG="${IMAGE_TAG:-$(git -C "$ROOT_DIR" rev-parse --short HEAD)}"

acr_server="$(az acr show --name "$ACR_NAME" --query loginServer --output tsv --only-show-errors)"
image="$acr_server/incidentops-rag-runtime:$IMAGE_TAG"

# ACR builds remotely so no model artifact is downloaded or executed locally.
az acr build \
  --registry "$ACR_NAME" \
  --image "incidentops-rag-runtime:$IMAGE_TAG" \
  --file "$ROOT_DIR/model_runtime/Dockerfile" \
  "$ROOT_DIR/model_runtime" \
  --only-show-errors

printf 'RAG_RUNTIME_IMAGE=%s\n' "$image"
