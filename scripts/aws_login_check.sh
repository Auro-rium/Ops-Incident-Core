#!/usr/bin/env bash
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
command -v aws >/dev/null 2>&1 || { echo "AWS CLI is required" >&2; exit 1; }
identity="$(aws sts get-caller-identity --region "$AWS_REGION" --output json)"
account="$(jq -r '.Account' <<<"$identity")"
arn="$(jq -r '.Arn' <<<"$identity")"
[[ "$account" =~ ^[0-9]{12}$ ]] || { echo "AWS authentication is not configured" >&2; exit 1; }
printf 'AWS authentication ready: account=%s principal=%s region=%s\n' "$account" "$arn" "$AWS_REGION"
