#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker-compose.ec2-demo.yml"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/ec2/.env.demo}"
RUNTIME_ENV_FILE="${RUNTIME_ENV_FILE:-$ROOT_DIR/deploy/ec2/.env.runtime}"
PUBLIC_URL="${PUBLIC_URL:-}"
COLLECTOR_REPO_PATH="${COLLECTOR_REPO_PATH:-}"
LOCAL_API_BASE="${LOCAL_API_BASE:-http://127.0.0.1/api}"

usage() {
  cat <<'EOF'
Usage: scripts/deploy_ec2_demo.sh [--public-url http://ec2-host] [--collector-repo ../OpsIncident-Collector]

Environment overrides:
  ENV_FILE              default deploy/ec2/.env.demo
  RUNTIME_ENV_FILE      default deploy/ec2/.env.runtime
  LOCAL_API_BASE        default http://127.0.0.1/api
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --public-url)
      PUBLIC_URL="${2:?--public-url requires a value}"
      shift 2
      ;;
    --collector-repo)
      COLLECTOR_REPO_PATH="${2:?--collector-repo requires a value}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Docker Engine and Docker Compose plugin are required. Run scripts/bootstrap_ec2.sh first." >&2
  exit 1
fi

mkdir -p "$ROOT_DIR/deploy/ec2/nginx/certs" "$ROOT_DIR/deploy/ec2/backups"

python3 - "$ROOT_DIR/deploy/ec2/.env.demo.example" "$ENV_FILE" "$PUBLIC_URL" "$COLLECTOR_REPO_PATH" <<'PY'
from __future__ import annotations

import secrets
import sys
from pathlib import Path

example = Path(sys.argv[1])
env_path = Path(sys.argv[2])
public_url = sys.argv[3].strip()
collector_repo = sys.argv[4].strip()


def parse_env(text: str) -> tuple[list[str], dict[str, str]]:
    lines = text.splitlines()
    values: dict[str, str] = {}
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key] = value
    return lines, values


template_lines, template_values = parse_env(example.read_text(encoding="utf-8"))
if env_path.exists():
    _, current = parse_env(env_path.read_text(encoding="utf-8"))
else:
    current = {}

values = {**template_values, **current}
if public_url:
    values["PUBLIC_BASE_URL"] = public_url.rstrip("/")
    values["FRONTEND_URL"] = public_url.rstrip("/")
    values["CORS_ALLOW_ORIGINS"] = public_url.rstrip("/")
    values["CORS_ORIGINS"] = public_url.rstrip("/")
if collector_repo:
    values["COLLECTOR_REPO_PATH"] = collector_repo

def needs_secret(key: str) -> bool:
    value = values.get(key, "")
    return not value or value.startswith("replace-with-")

if needs_secret("POSTGRES_PASSWORD"):
    values["POSTGRES_PASSWORD"] = secrets.token_urlsafe(32)
if needs_secret("JWT_SECRET"):
    values["JWT_SECRET"] = secrets.token_urlsafe(48)
if needs_secret("BOOTSTRAP_ADMIN_PASSWORD"):
    values["BOOTSTRAP_ADMIN_PASSWORD"] = secrets.token_urlsafe(24)

values["DATABASE_URL"] = (
    f"postgresql+asyncpg://{values.get('POSTGRES_USER', 'incidentops')}:"
    f"{values['POSTGRES_PASSWORD']}@postgres:5432/{values.get('POSTGRES_DB', 'incidentops')}"
)
values["APP_ENV"] = "production"
values["DB_CREATE_ALL"] = "false"
values["DB_REQUIRE_MIGRATIONS"] = "true"
values["LOCAL_INGEST_ENABLED"] = "false"
values["ENABLE_LOCAL_INGEST"] = "false"
values["ALLOW_DEMO_PROJECT_BYPASS"] = "false"
values["ALLOW_LOCAL_SEED_ADMIN"] = "false"
values["DEMO_MODE_PUBLIC"] = "false"
values["WORKER_MODE"] = "queue"
values["JOB_QUEUE_BACKEND"] = "redis"
values["RATE_LIMIT_BACKEND"] = "redis"
values["METRICS_BACKEND"] = "prometheus"
values["NEXT_PUBLIC_API_BASE_URL"] = "/api"

ordered_keys = []
for line in template_lines:
    raw = line.strip()
    if raw and not raw.startswith("#") and "=" in raw:
        ordered_keys.append(raw.split("=", 1)[0])
for key in sorted(values):
    if key not in ordered_keys:
        ordered_keys.append(key)

rendered = []
for line in template_lines:
    raw = line.strip()
    if raw and not raw.startswith("#") and "=" in raw:
        key = raw.split("=", 1)[0]
        rendered.append(f"{key}={values[key]}")
    else:
        rendered.append(line)

seen = {line.split("=", 1)[0] for line in rendered if line.strip() and not line.strip().startswith("#") and "=" in line}
for key in ordered_keys:
    if key not in seen:
        rendered.append(f"{key}={values[key]}")

env_path.write_text("\n".join(rendered).rstrip() + "\n", encoding="utf-8")
env_path.chmod(0o600)
print(f"Prepared {env_path}")
PY

if [[ ! -f "$ROOT_DIR/deploy/ec2/nginx/certs/selfsigned.crt" || ! -f "$ROOT_DIR/deploy/ec2/nginx/certs/selfsigned.key" ]]; then
  openssl req -x509 -nodes -days 14 -newkey rsa:2048 \
    -keyout "$ROOT_DIR/deploy/ec2/nginx/certs/selfsigned.key" \
    -out "$ROOT_DIR/deploy/ec2/nginx/certs/selfsigned.crt" \
    -subj "/CN=incidentops-ec2-demo" >/dev/null 2>&1
  chmod 600 "$ROOT_DIR/deploy/ec2/nginx/certs/selfsigned.key"
  echo "Generated temporary self-signed HTTPS certificate."
fi

touch "$RUNTIME_ENV_FILE"
chmod 600 "$RUNTIME_ENV_FILE"

compose() {
  EC2_DEMO_ENV_FILE="$ENV_FILE" EC2_DEMO_RUNTIME_ENV_FILE="$RUNTIME_ENV_FILE" \
    docker compose --env-file "$ENV_FILE" --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

echo "Building images..."
compose build api core-worker frontend collector nginx

echo "Starting Postgres and Redis..."
compose up -d postgres redis

echo "Running Alembic migrations and schema checks..."
compose --profile tools run --rm migrate

echo "Starting Core API, worker, frontend, and Nginx..."
compose up -d api core-worker frontend nginx

echo "Bootstrapping admin user..."
compose --profile tools run --rm admin-bootstrap

echo "Waiting for Core readiness through Nginx..."
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1/ready" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS "http://127.0.0.1/ready" >/dev/null

echo "Creating demo project and runtime Collector token..."
python3 - "$ENV_FILE" "$RUNTIME_ENV_FILE" "$LOCAL_API_BASE" <<'PY'
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

env_path = Path(sys.argv[1])
runtime_path = Path(sys.argv[2])
api_base = sys.argv[3].rstrip("/")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key] = value
    return values


def request(method: str, path: str, payload: dict | None = None, token: str | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{api_base}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{method} {path} failed: {exc.code} {body}") from exc


values = read_env(env_path)
login = request(
    "POST",
    "/v1/auth/login",
    {
        "email": values["BOOTSTRAP_ADMIN_EMAIL"],
        "password": values["BOOTSTRAP_ADMIN_PASSWORD"],
    },
)
token = login["access_token"]
project = request(
    "POST",
    "/v1/projects",
    {"name": f"ec2-demo-{int(time.time())}", "demo_mode": False},
    token,
)
project_id = project["project_id"]
runtime_path.write_text(
    "\n".join(
        [
            f"DEMO_PROJECT_ID={project_id}",
            f"INCIDENTOPS_PROJECT_ID={project_id}",
            f"PROJECT_ID={project_id}",
            f"INCIDENTOPS_TOKEN={token}",
        ]
    )
    + "\n",
    encoding="utf-8",
)
runtime_path.chmod(0o600)
print(f"Created demo project {project_id}")
PY

echo "Starting Collector daemon..."
compose up -d --force-recreate collector

echo "Waiting for Collector health..."
for _ in $(seq 1 90); do
  if compose exec -T collector opsincident-collector daemon health --host 127.0.0.1 --port 8686 >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
compose exec -T collector opsincident-collector daemon health --host 127.0.0.1 --port 8686

echo "EC2 demo deployment is running."
echo "Frontend: $(grep '^PUBLIC_BASE_URL=' "$ENV_FILE" | cut -d= -f2-)"
echo "API health: $(grep '^PUBLIC_BASE_URL=' "$ENV_FILE" | cut -d= -f2-)/health"
echo "Run scripts/smoke_ec2_demo.sh to validate search, investigate, workflow, and Collector sync."
