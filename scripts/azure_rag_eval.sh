#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AZURE_RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
NAME_PREFIX="${NAME_PREFIX:?NAME_PREFIX is required}"
BENCHMARK_JOB_NAME="${BENCHMARK_JOB_NAME:-${NAME_PREFIX}-benchmark-job}"

execution_name="$(az containerapp job start --resource-group "$AZURE_RESOURCE_GROUP" --name "$BENCHMARK_JOB_NAME" --query name --output tsv --only-show-errors)"
printf 'Started benchmark execution %s\n' "$execution_name"

for _ in $(seq 1 120); do
  status="$(az containerapp job execution show --resource-group "$AZURE_RESOURCE_GROUP" --job-name "$BENCHMARK_JOB_NAME" --name "$execution_name" --query properties.status --output tsv --only-show-errors)"
  case "$status" in
    Succeeded) az containerapp job logs show --resource-group "$AZURE_RESOURCE_GROUP" --name "$BENCHMARK_JOB_NAME" --tail 200 --follow false --only-show-errors; exit 0 ;;
    Failed) az containerapp job logs show --resource-group "$AZURE_RESOURCE_GROUP" --name "$BENCHMARK_JOB_NAME" --tail 200 --follow false --only-show-errors >&2; exit 1 ;;
  esac
  sleep 5
done

echo "Timed out waiting for Azure RAG evaluation job" >&2
exit 1
