#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/ec2/.env.demo}"
EXAMPLE_ENV="$ROOT_DIR/deploy/ec2/.env.demo.example"

install_docker_apt() {
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl git openssl python3
  if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sudo sh
  fi
}

install_docker_yum() {
  if command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y docker git openssl python3
  else
    sudo yum install -y docker git openssl python3
  fi
  sudo systemctl enable --now docker
}

if ! command -v docker >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    install_docker_apt
  elif command -v yum >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1; then
    install_docker_yum
  else
    echo "Unsupported OS: install Docker Engine and Docker Compose plugin manually." >&2
    exit 1
  fi
fi

sudo systemctl enable --now docker

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose plugin is required but was not found after Docker install." >&2
  exit 1
fi

mkdir -p "$ROOT_DIR/deploy/ec2/backups" "$ROOT_DIR/deploy/ec2/nginx/certs"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$EXAMPLE_ENV" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "Created $ENV_FILE from example. Edit PUBLIC_BASE_URL, FRONTEND_URL, and admin email before deploy."
fi

if [[ -n "${USER:-}" ]]; then
  sudo usermod -aG docker "$USER" || true
fi

echo "EC2 bootstrap complete."
echo "If your user was newly added to the docker group, log out and back in before running deploy."
