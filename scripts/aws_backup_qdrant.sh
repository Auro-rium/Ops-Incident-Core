#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$ROOT_DIR/infra/aws/terraform"
AWS_REGION="${AWS_REGION:-us-east-1}"
vault="$(terraform -chdir="$TF_DIR" output -raw backup_vault_name)"
role="$(terraform -chdir="$TF_DIR" output -raw backup_role_arn)"
resource="$(terraform -chdir="$TF_DIR" output -raw qdrant_instance_arn)"
job="$(aws backup start-backup-job --region "$AWS_REGION" --backup-vault-name "$vault" \
  --resource-arn "$resource" --iam-role-arn "$role" --query BackupJobId --output text)"
printf 'Started Qdrant backup job %s in vault %s\n' "$job" "$vault"
