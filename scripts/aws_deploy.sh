#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$ROOT_DIR/infra/aws/terraform"
AWS_REGION="${AWS_REGION:-us-east-1}"
TF_STATE_BUCKET="${TF_STATE_BUCKET:?TF_STATE_BUCKET is required}"
TF_STATE_KEY="${TF_STATE_KEY:-incidentops/production.tfstate}"

"$ROOT_DIR/scripts/aws_login_check.sh"
terraform -chdir="$TF_DIR" init -input=false -reconfigure \
  -backend-config="bucket=$TF_STATE_BUCKET" \
  -backend-config="key=$TF_STATE_KEY" \
  -backend-config="region=$AWS_REGION" \
  -backend-config="encrypt=true" \
  -backend-config="use_lockfile=true"
terraform -chdir="$TF_DIR" fmt -check -recursive
terraform -chdir="$TF_DIR" validate
terraform -chdir="$TF_DIR" apply -input=false "$@"
