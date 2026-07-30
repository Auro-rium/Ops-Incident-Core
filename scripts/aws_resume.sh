#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$ROOT_DIR/infra/aws/terraform"
AWS_REGION="${AWS_REGION:-us-east-1}"
TERRAFORM_BIN="${TERRAFORM_BIN:-terraform}"
RUN_MIGRATIONS="${RUN_MIGRATIONS:-true}"
RUN_MODEL_PREFLIGHT="${RUN_MODEL_PREFLIGHT:-false}"
RUN_SMOKE="${RUN_SMOKE:-true}"

"$ROOT_DIR/scripts/aws_login_check.sh"

database_identifier="$($TERRAFORM_BIN -chdir="$TF_DIR" output -raw database_identifier)"
database_state="$(aws rds describe-db-instances \
  --region "$AWS_REGION" \
  --db-instance-identifier "$database_identifier" \
  --query 'DBInstances[0].DBInstanceStatus' \
  --output text)"
case "$database_state" in
  stopped)
    aws rds start-db-instance \
      --region "$AWS_REGION" \
      --db-instance-identifier "$database_identifier" >/dev/null
    ;;
  available) ;;
  *) printf 'Waiting for RDS %s from state %s\n' "$database_identifier" "$database_state" ;;
esac
aws rds wait db-instance-available \
  --region "$AWS_REGION" \
  --db-instance-identifier "$database_identifier"
printf 'RDS instance %s is available\n' "$database_identifier"

qdrant_instance_id="$($TERRAFORM_BIN -chdir="$TF_DIR" output -raw qdrant_instance_id)"
qdrant_state="$(aws ec2 describe-instances \
  --region "$AWS_REGION" \
  --instance-ids "$qdrant_instance_id" \
  --query 'Reservations[0].Instances[0].State.Name' \
  --output text)"
if [[ "$qdrant_state" == "stopped" ]]; then
  aws ec2 start-instances --region "$AWS_REGION" --instance-ids "$qdrant_instance_id" >/dev/null
fi
aws ec2 wait instance-running --region "$AWS_REGION" --instance-ids "$qdrant_instance_id"
aws ec2 wait instance-status-ok --region "$AWS_REGION" --instance-ids "$qdrant_instance_id"
printf 'Qdrant EC2 instance %s passed EC2 status checks\n' "$qdrant_instance_id"

if [[ "$RUN_MIGRATIONS" == "true" ]]; then
  "$ROOT_DIR/scripts/aws_run_migrations.sh"
fi
if [[ "$RUN_MODEL_PREFLIGHT" == "true" ]]; then
  "$ROOT_DIR/scripts/aws_model_preflight.sh"
fi

"$ROOT_DIR/scripts/aws_deploy_services.sh"

if [[ "$RUN_SMOKE" == "true" ]]; then
  "$ROOT_DIR/scripts/aws_smoke.sh"
fi

public_url="$($TERRAFORM_BIN -chdir="$TF_DIR" output -raw public_url)"
printf 'IncidentOps resumed: %s\n' "$public_url"
