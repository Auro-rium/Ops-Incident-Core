#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker-compose.ec2-demo.yml"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/deploy/ec2/.env.demo}"
RUNTIME_ENV_FILE="${RUNTIME_ENV_FILE:-$ROOT_DIR/deploy/ec2/.env.runtime}"
API_BASE_URL="${API_BASE_URL:-http://127.0.0.1/api}"
QUERY="${QUERY:-What evidence exists for timeout or deploy-related failures?}"

if [[ ! -f "$ENV_FILE" || ! -f "$RUNTIME_ENV_FILE" ]]; then
  echo "Missing $ENV_FILE or $RUNTIME_ENV_FILE. Run scripts/deploy_ec2_demo.sh first." >&2
  exit 1
fi

compose() {
  EC2_DEMO_ENV_FILE="$ENV_FILE" EC2_DEMO_RUNTIME_ENV_FILE="$RUNTIME_ENV_FILE" \
    docker compose --env-file "$ENV_FILE" --env-file "$RUNTIME_ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

echo "Checking public health/readiness..."
curl -fsS "${API_BASE_URL%/api}/" | grep -qi "<html"
curl -fsS "${API_BASE_URL%/api}/health" >/dev/null
curl -fsS "${API_BASE_URL%/api}/ready" >/dev/null
curl -fsS "$API_BASE_URL/v1/capabilities" >/dev/null

echo "Checking Collector health..."
COLLECTOR_HEALTH="$(compose exec -T collector opsincident-collector daemon health --host 127.0.0.1 --port 8686)"
printf '%s\n' "$COLLECTOR_HEALTH"
printf '%s' "$COLLECTOR_HEALTH" | python3 -c 'import json,sys; p=json.load(sys.stdin); assert p.get("core_reachable") is True, p'

read_env_key() {
  local file="$1"
  local key="$2"
  python3 - "$file" "$key" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

path = Path(sys.argv[1])
target = sys.argv[2]
for line in path.read_text(encoding="utf-8").splitlines():
    raw = line.strip()
    if not raw or raw.startswith("#") or "=" not in raw:
        continue
    key, value = raw.split("=", 1)
    if key == target:
        print(value)
        break
PY
}

DEMO_PROJECT_ID="$(read_env_key "$RUNTIME_ENV_FILE" DEMO_PROJECT_ID)"
COLLECTOR_SOURCE_NAME_VALUE="$(read_env_key "$ENV_FILE" COLLECTOR_SOURCE_NAME)"
COLLECTOR_SOURCE_NAME_VALUE="${COLLECTOR_SOURCE_NAME_VALUE:-ec2-demo-fixture}"

echo "Forcing one Collector sync cycle..."
compose run --rm collector sync \
  --path /app/tests/fixtures/basic_project \
  --export api \
  --api-url http://api:8000 \
  --project-id "$DEMO_PROJECT_ID" \
  --source-name "$COLLECTOR_SOURCE_NAME_VALUE" \
  --source-type filesystem \
  --yes \
  --force >/tmp/incidentops-collector-sync.json
cat /tmp/incidentops-collector-sync.json

echo "Running Core search, investigate, and workflow checks..."
python3 - "$ENV_FILE" "$RUNTIME_ENV_FILE" "$API_BASE_URL" "$QUERY" <<'PY'
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
query = sys.argv[4]


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
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{method} {path} failed: {exc.code} {body}") from exc


env = read_env(env_path)
runtime = read_env(runtime_path)
project_id = runtime["DEMO_PROJECT_ID"]
login = request(
    "POST",
    "/v1/auth/login",
    {"email": env["BOOTSTRAP_ADMIN_EMAIL"], "password": env["BOOTSTRAP_ADMIN_PASSWORD"]},
)
token = login["access_token"]

search = request("POST", "/v1/search", {"project_id": project_id, "query": query, "top_k": 8}, token)
results = search.get("results", [])
if not results:
    raise SystemExit("search returned zero evidence")

investigation = request(
    "POST",
    "/v1/investigate",
    {"project_id": project_id, "query": query, "top_k": 8},
    token,
)
if not investigation.get("citations"):
    raise SystemExit("investigation returned no citations")

run = request(
    "POST",
    "/v1/runs",
    {"project_id": project_id, "query": query, "top_k": 8, "create_issue_draft": True},
    token,
)
run_id = run["run_id"]
status = run.get("status")
for _ in range(30):
    state = request("GET", f"/v1/runs/{run_id}", token=token)
    status = state.get("status")
    if status in {"completed", "awaiting_approval", "waiting_for_approval", "failed"}:
        break
    time.sleep(2)
if status == "failed":
    raise SystemExit(f"workflow run failed: {run_id}")

print(
    json.dumps(
        {
            "project_id": project_id,
            "search_results": len(results),
            "top_evidence_paths": [item.get("document_path") for item in results[:3]],
            "investigation_confidence": investigation.get("confidence"),
            "citations": len(investigation.get("citations", [])),
            "run_id": run_id,
            "run_status": status,
        },
        indent=2,
    )
)
PY

echo "EC2 demo smoke passed."
