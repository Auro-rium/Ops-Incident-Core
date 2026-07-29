#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$ROOT_DIR/infra/aws/terraform"
AWS_REGION="${AWS_REGION:-us-east-1}"
TASK_DEFINITION="${TASK_DEFINITION:?TASK_DEFINITION output name is required}"
CONTAINER_NAME="${CONTAINER_NAME:?CONTAINER_NAME is required}"
TASK_COMMAND="${TASK_COMMAND:?TASK_COMMAND is required}"

cluster="$(terraform -chdir="$TF_DIR" output -raw ecs_cluster_name)"
task_definition="$(terraform -chdir="$TF_DIR" output -raw "$TASK_DEFINITION")"
subnets="$(terraform -chdir="$TF_DIR" output -json private_subnet_ids)"
security_group="$(terraform -chdir="$TF_DIR" output -raw ecs_security_group_id)"
network="$(jq -cn --argjson subnets "$subnets" --arg sg "$security_group" '{awsvpcConfiguration:{subnets:$subnets,securityGroups:[$sg],assignPublicIp:"DISABLED"}}')"
overrides="$(jq -cn --arg name "$CONTAINER_NAME" --arg command "$TASK_COMMAND" '{containerOverrides:[{name:$name,command:["sh","-lc",$command]}]}')"

result="$(aws ecs run-task --region "$AWS_REGION" --cluster "$cluster" --launch-type FARGATE \
  --task-definition "$task_definition" --network-configuration "$network" --overrides "$overrides" --output json)"
task_arn="$(jq -r '.tasks[0].taskArn // empty' <<<"$result")"
if [[ -z "$task_arn" ]]; then
  jq '{failures: .failures}' <<<"$result" >&2
  exit 1
fi
printf 'Started one-off task %s\n' "${task_arn##*/}"
aws ecs wait tasks-stopped --region "$AWS_REGION" --cluster "$cluster" --tasks "$task_arn"
description="$(aws ecs describe-tasks --region "$AWS_REGION" --cluster "$cluster" --tasks "$task_arn" --output json)"
exit_code="$(jq -r --arg name "$CONTAINER_NAME" '.tasks[0].containers[] | select(.name==$name) | .exitCode // 1' <<<"$description")"
if [[ "$exit_code" != "0" ]]; then
  jq '{stoppedReason: .tasks[0].stoppedReason, containers: [.tasks[0].containers[] | {name, reason, exitCode}]}' <<<"$description" >&2
  exit "$exit_code"
fi
printf 'One-off task completed successfully\n'
