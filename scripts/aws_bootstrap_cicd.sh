#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AWS_REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${AWS_BOOTSTRAP_STACK_NAME:-incidentops-cicd-bootstrap}"
REPOSITORY="${GITHUB_REPOSITORY:-Auro-rium/Ops-Incident-Core}"
GITHUB_ENVIRONMENT="${GITHUB_ENVIRONMENT:-aws-production}"
STATE_BUCKET="${TF_STATE_BUCKET:-incidentops-tfstate-$(aws sts get-caller-identity --query Account --output text)-$AWS_REGION}"

"$ROOT_DIR/scripts/aws_login_check.sh"

aws cloudformation deploy \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --template-file "$ROOT_DIR/infra/aws/bootstrap/github-oidc.yml" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "Repository=$REPOSITORY" \
    "Environment=$GITHUB_ENVIRONMENT" \
    "StateBucketName=$STATE_BUCKET"

role_arn="$(aws cloudformation describe-stacks \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='DeploymentRoleArn'].OutputValue" \
  --output text)"

printf 'AWS bootstrap stack: %s\n' "$STACK_NAME"
printf 'Terraform state bucket: %s\n' "$STATE_BUCKET"
printf 'GitHub deployment role: %s\n' "$role_arn"

if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  gh variable set AWS_REGION --repo "$REPOSITORY" --body "$AWS_REGION"
  gh variable set AWS_ROLE_TO_ASSUME --repo "$REPOSITORY" --body "$role_arn"
  gh variable set TF_STATE_BUCKET --repo "$REPOSITORY" --body "$STATE_BUCKET"
  gh variable set TF_STATE_KEY --repo "$REPOSITORY" --body "incidentops/production.tfstate"
  printf 'Configured non-secret GitHub Actions repository variables.\n'
else
  printf 'GitHub CLI is unavailable; configure the printed role and state bucket as repository variables.\n'
fi
