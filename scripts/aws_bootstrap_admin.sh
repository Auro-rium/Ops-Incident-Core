#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASK_DEFINITION=bootstrap_task_definition_arn CONTAINER_NAME=bootstrap \
  TASK_COMMAND='python -m incidentops.security.bootstrap_admin' \
  "$ROOT_DIR/scripts/aws_run_oneoff.sh"
