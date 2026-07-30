#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
BEDROCK_CHAT_MODEL_ID="${BEDROCK_CHAT_MODEL_ID:-${TF_VAR_bedrock_chat_model_id:-}}"
BEDROCK_EMBEDDING_MODEL_ID="${BEDROCK_EMBEDDING_MODEL_ID:-${TF_VAR_bedrock_embedding_model_id:-}}"

: "${BEDROCK_CHAT_MODEL_ID:?BEDROCK_CHAT_MODEL_ID or TF_VAR_bedrock_chat_model_id is required}"
: "${BEDROCK_EMBEDDING_MODEL_ID:?BEDROCK_EMBEDDING_MODEL_ID or TF_VAR_bedrock_embedding_model_id is required}"

check_model() {
  local role="$1"
  local model_id="$2"
  local authorization
  local region_availability

  authorization="$(aws bedrock get-foundation-model-availability \
    --region "$AWS_REGION" \
    --model-id "$model_id" \
    --query authorizationStatus \
    --output text)"
  region_availability="$(aws bedrock get-foundation-model-availability \
    --region "$AWS_REGION" \
    --model-id "$model_id" \
    --query regionAvailability \
    --output text)"

  if [[ "$authorization" != "AUTHORIZED" || "$region_availability" != "AVAILABLE" ]]; then
    printf '%s model is unavailable: authorization=%s region=%s model=%s\n' \
      "$role" "$authorization" "$region_availability" "$model_id" >&2
    printf 'Resolve Bedrock account/model access before creating or promoting billable runtime resources.\n' >&2
    return 1
  fi
  printf '%s model access authorized: %s\n' "$role" "$model_id"
}

check_model embedding "$BEDROCK_EMBEDDING_MODEL_ID"
check_model chat "$BEDROCK_CHAT_MODEL_ID"
