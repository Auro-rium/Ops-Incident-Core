#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$ROOT_DIR/infra/aws/terraform"
AWS_REGION="${AWS_REGION:-us-east-1}"
cluster="$(terraform -chdir="$TF_DIR" output -raw ecs_cluster_name)"
desired="$(terraform -chdir="$TF_DIR" output -json desired_counts)"

services=(api worker frontend collector mcp)
for component in "${services[@]}"; do
  service="$(terraform -chdir="$TF_DIR" output -raw "${component}_service_name")"
  task_definition="$(terraform -chdir="$TF_DIR" output -raw "${component}_task_definition_arn")"
  count="$(jq -r --arg component "$component" '.[$component]' <<<"$desired")"
  aws ecs update-service --region "$AWS_REGION" --cluster "$cluster" --service "$service" \
    --task-definition "$task_definition" --desired-count "$count" --force-new-deployment >/dev/null
  printf 'Promoted %s to %s task(s)\n' "$component" "$count"
done

for component in api worker frontend; do
  service="$(terraform -chdir="$TF_DIR" output -raw "${component}_service_name")"
  aws ecs wait services-stable --region "$AWS_REGION" --cluster "$cluster" --services "$service"
done
printf 'Required ECS services are stable\n'
