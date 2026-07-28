#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

AZURE_RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
CONFIRM="${CONFIRM:-}"

"$ROOT_DIR/scripts/azure_login_check.sh"

if [[ "$CONFIRM" != "delete-$AZURE_RESOURCE_GROUP" ]]; then
  cat >&2 <<EOF
This deletes the Azure resource group '$AZURE_RESOURCE_GROUP' and all demo resources.

Run again with:
  CONFIRM=delete-$AZURE_RESOURCE_GROUP AZURE_RESOURCE_GROUP=$AZURE_RESOURCE_GROUP scripts/azure_teardown.sh
EOF
  exit 2
fi

az group delete \
  --name "$AZURE_RESOURCE_GROUP" \
  --yes \
  --no-wait \
  --only-show-errors

echo "Deletion started for resource group '$AZURE_RESOURCE_GROUP'. Billing stops after resources finish deleting."
