#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker-compose.ec2-demo.yml"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/ec2/.env.demo}"
RUNTIME_ENV_FILE="${RUNTIME_ENV_FILE:-$ROOT_DIR/deploy/ec2/.env.runtime}"
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/deploy/ec2/backups}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE" >&2
  exit 1
fi

set -a
source "$ENV_FILE"
if [[ -f "$RUNTIME_ENV_FILE" ]]; then
  source "$RUNTIME_ENV_FILE"
fi
set +a

mkdir -p "$BACKUP_DIR"
OUT="$BACKUP_DIR/incidentops-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"

EC2_DEMO_ENV_FILE="$ENV_FILE" EC2_DEMO_RUNTIME_ENV_FILE="$RUNTIME_ENV_FILE" \
  docker compose --env-file "$ENV_FILE" --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" \
  exec -T postgres pg_dump -U "${POSTGRES_USER:-incidentops}" "${POSTGRES_DB:-incidentops}" | gzip > "$OUT"

chmod 600 "$OUT"
echo "Wrote $OUT"
