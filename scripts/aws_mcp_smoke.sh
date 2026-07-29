#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$ROOT_DIR/infra/aws/terraform"
PROJECT_ID="${PROJECT_ID:-${INCIDENTOPS_PROJECT_ID:-}}"
[[ -n "$PROJECT_ID" ]] || { echo "PROJECT_ID is required for MCP smoke" >&2; exit 1; }
MCP_URL="$(terraform -chdir="$TF_DIR" output -raw mcp_private_url)"
command="INCIDENTOPS_PROJECT_ID='$PROJECT_ID' MCP_URL='$MCP_URL' python scripts/aws_mcp_smoke.py"
TASK_DEFINITION=mcp_task_definition_arn CONTAINER_NAME=mcp TASK_COMMAND="$command" \
  "$ROOT_DIR/scripts/aws_run_oneoff.sh"
