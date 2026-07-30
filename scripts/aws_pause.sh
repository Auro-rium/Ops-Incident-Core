#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$ROOT_DIR/infra/aws/terraform"
AWS_REGION="${AWS_REGION:-us-east-1}"
TERRAFORM_BIN="${TERRAFORM_BIN:-terraform}"

"$ROOT_DIR/scripts/aws_login_check.sh"

cluster="$($TERRAFORM_BIN -chdir="$TF_DIR" output -raw ecs_cluster_name)"
services=(api worker frontend collector mcp)
service_names=()
for component in "${services[@]}"; do
  service="$($TERRAFORM_BIN -chdir="$TF_DIR" output -raw "${component}_service_name")"
  service_names+=("$service")
  aws ecs update-service \
    --region "$AWS_REGION" \
    --cluster "$cluster" \
    --service "$service" \
    --desired-count 0 >/dev/null
  printf 'Scaled %s to zero tasks\n' "$component"
done
aws ecs wait services-stable \
  --region "$AWS_REGION" \
  --cluster "$cluster" \
  --services "${service_names[@]}"

qdrant_instance_id="$($TERRAFORM_BIN -chdir="$TF_DIR" output -raw qdrant_instance_id)"
qdrant_state="$(aws ec2 describe-instances \
  --region "$AWS_REGION" \
  --instance-ids "$qdrant_instance_id" \
  --query 'Reservations[0].Instances[0].State.Name' \
  --output text)"
case "$qdrant_state" in
  running|pending)
    aws ec2 stop-instances --region "$AWS_REGION" --instance-ids "$qdrant_instance_id" >/dev/null
    aws ec2 wait instance-stopped --region "$AWS_REGION" --instance-ids "$qdrant_instance_id"
    printf 'Stopped Qdrant EC2 instance %s\n' "$qdrant_instance_id"
    ;;
  stopped)
    printf 'Qdrant EC2 instance %s is already stopped\n' "$qdrant_instance_id"
    ;;
  *)
    printf 'Qdrant EC2 instance %s is in state %s; no stop action taken\n' "$qdrant_instance_id" "$qdrant_state" >&2
    ;;
esac

database_identifier="$($TERRAFORM_BIN -chdir="$TF_DIR" output -raw database_identifier)"
database_state="$(aws rds describe-db-instances \
  --region "$AWS_REGION" \
  --db-instance-identifier "$database_identifier" \
  --query 'DBInstances[0].DBInstanceStatus' \
  --output text)"
case "$database_state" in
  available)
    aws rds stop-db-instance \
      --region "$AWS_REGION" \
      --db-instance-identifier "$database_identifier" >/dev/null
    aws rds wait db-instance-stopped \
      --region "$AWS_REGION" \
      --db-instance-identifier "$database_identifier"
    printf 'Stopped RDS instance %s\n' "$database_identifier"
    ;;
  stopping)
    aws rds wait db-instance-stopped \
      --region "$AWS_REGION" \
      --db-instance-identifier "$database_identifier"
    printf 'RDS instance %s finished stopping\n' "$database_identifier"
    ;;
  stopped)
    printf 'RDS instance %s is already stopped\n' "$database_identifier"
    ;;
  *)
    printf 'RDS instance %s is in state %s; no stop action taken\n' "$database_identifier" "$database_state" >&2
    ;;
esac

cat <<'EOF'
IncidentOps runtime is paused.

Charges still continue for resources that AWS cannot pause: ElastiCache,
NAT Gateway, ALB, EBS volumes, snapshots/backups, ECR, logs, Secrets Manager,
and Terraform state storage. Use scripts/aws_teardown.sh for a full destroy
after preserving any data that must survive.
EOF
