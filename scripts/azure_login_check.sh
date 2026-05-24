#!/usr/bin/env bash
set -euo pipefail

AZURE_SUBSCRIPTION_ID="${AZURE_SUBSCRIPTION_ID:-}"

if ! command -v az >/dev/null 2>&1; then
  echo "Azure CLI is required. Install az before deploying." >&2
  exit 1
fi

account_json="$(az account show --only-show-errors 2>/dev/null || true)"
if [[ -z "$account_json" ]]; then
  echo "Azure CLI is not logged in. Run: az login" >&2
  exit 1
fi

if [[ -n "$AZURE_SUBSCRIPTION_ID" ]]; then
  az account set --subscription "$AZURE_SUBSCRIPTION_ID" --only-show-errors
fi

az account show --query '{name:name, id:id, tenantId:tenantId, user:user.name}' --output table
