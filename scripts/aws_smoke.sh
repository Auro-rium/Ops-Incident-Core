#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$ROOT_DIR/infra/aws/terraform"
AWS_REGION="${AWS_REGION:-us-east-1}"
public_url="$(terraform -chdir="$TF_DIR" output -raw public_url)"
secret_arn="$(terraform -chdir="$TF_DIR" output -raw runtime_secret_arn)"
secret_json="$(aws secretsmanager get-secret-value --region "$AWS_REGION" --secret-id "$secret_arn" --query SecretString --output text)"
email="$(jq -r '.bootstrap_admin_email' <<<"$secret_json")"
password="$(jq -r '.bootstrap_admin_password' <<<"$secret_json")"
unset secret_json

curl --fail --silent --show-error "$public_url/api/health" >/dev/null
curl --fail --silent --show-error "$public_url/api/ready" >/dev/null
uv run python "$ROOT_DIR/scripts/smoke_prod.py" \
  --base-url "$public_url/api" \
  --email "$email" \
  --password "$password" \
  --query "What does this tiny service evidence say?"
unset password
printf 'AWS smoke passed: %s\n' "$public_url"
