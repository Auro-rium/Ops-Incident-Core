#!/usr/bin/env bash
set -euo pipefail

[[ "${ALLOW_AWS_DESTROY:-no}" == "yes" ]] || {
  echo "Refusing destroy. Set ALLOW_AWS_DESTROY=yes after backups and deletion-protection review." >&2
  exit 1
}
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "RDS deletion protection and retained Qdrant EBS volumes can intentionally block or survive teardown." >&2
terraform -chdir="$ROOT_DIR/infra/aws/terraform" destroy
