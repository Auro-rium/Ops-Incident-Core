#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker-compose.ec2-demo.yml"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/ec2/.env.demo}"
RUNTIME_ENV_FILE="${RUNTIME_ENV_FILE:-$ROOT_DIR/deploy/ec2/.env.runtime}"
REMOVE_VOLUMES=false

if [[ "${1:-}" == "--volumes" ]]; then
  REMOVE_VOLUMES=true
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE" >&2
  exit 1
fi
touch "$RUNTIME_ENV_FILE"

if [[ "$REMOVE_VOLUMES" == "true" ]]; then
  echo "Stopping containers and removing volumes..."
  EC2_DEMO_ENV_FILE="$ENV_FILE" EC2_DEMO_RUNTIME_ENV_FILE="$RUNTIME_ENV_FILE" \
    docker compose --env-file "$ENV_FILE" --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" down --volumes
else
  echo "Stopping containers. Volumes are preserved."
  EC2_DEMO_ENV_FILE="$ENV_FILE" EC2_DEMO_RUNTIME_ENV_FILE="$RUNTIME_ENV_FILE" \
    docker compose --env-file "$ENV_FILE" --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" down
fi

echo "Teardown complete."
