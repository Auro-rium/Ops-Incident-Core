#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASK_DEFINITION=migration_task_definition_arn CONTAINER_NAME=migration \
  TASK_COMMAND='alembic upgrade head && python scripts/check_migrations.py' \
  "$ROOT_DIR/scripts/aws_run_oneoff.sh"
