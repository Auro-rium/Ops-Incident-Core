#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

AZURE_RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
NAME_PREFIX="${NAME_PREFIX:?NAME_PREFIX is required}"
CORE_API_APP_NAME="${CORE_API_APP_NAME:-${NAME_PREFIX}-core-api}"
COLLECTOR_APP_NAME="${COLLECTOR_APP_NAME:-${NAME_PREFIX}-collector}"
MCP_APP_NAME="${MCP_APP_NAME:-${NAME_PREFIX}-mcp}"
API_URL="${API_URL:-}"
FRONTEND_URL="${FRONTEND_URL:-}"
SMOKE_EMAIL="${SMOKE_EMAIL:-${BOOTSTRAP_ADMIN_EMAIL:-admin@incidentops.local}}"
SMOKE_PASSWORD="${SMOKE_PASSWORD:-${BOOTSTRAP_ADMIN_PASSWORD:-}}"
SMOKE_QUERY="${SMOKE_QUERY:-What does this tiny service evidence say?}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-240}"
WORK_FILE="$(mktemp)"
trap 'rm -f "$WORK_FILE"' EXIT

"$ROOT_DIR/scripts/azure_login_check.sh"

if [[ -z "$API_URL" ]]; then
  API_URL="$(az containerapp show \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$CORE_API_APP_NAME" \
    --query 'properties.configuration.ingress.fqdn' \
    --output tsv \
    --only-show-errors)"
  API_URL="https://$API_URL"
fi

if [[ -z "$FRONTEND_URL" ]]; then
  FRONTEND_URL="$(az containerapp show \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name incidentops-frontend \
    --query 'properties.configuration.ingress.fqdn' \
    --output tsv \
    --only-show-errors 2>/dev/null || true)"
  [[ -n "$FRONTEND_URL" ]] && FRONTEND_URL="https://$FRONTEND_URL"
fi

if [[ -z "$SMOKE_PASSWORD" ]]; then
  echo "SMOKE_PASSWORD or BOOTSTRAP_ADMIN_PASSWORD is required." >&2
  exit 1
fi

python3 - "$API_URL" "$FRONTEND_URL" "$SMOKE_EMAIL" "$SMOKE_PASSWORD" "$WORK_FILE" <<'PY'
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

api_url, frontend_url, email, password, work_file = sys.argv[1:6]
api_url = api_url.rstrip("/")


def request(method: str, path_or_url: str, *, token: str | None = None, payload: dict | None = None):
    url = path_or_url if path_or_url.startswith("http") else f"{api_url}{path_or_url}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            if not body:
                return {}
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {"_raw": body[:300]}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{method} {url} failed with HTTP {exc.code}: {body[:300]}") from exc


request("GET", "/health")
request("GET", "/ready")
request("GET", "/v1/capabilities")
login = request("POST", "/v1/auth/login", payload={"email": email, "password": password})
token = login["access_token"]
project = request(
    "POST",
    "/v1/projects",
    token=token,
    payload={"name": f"azure-smoke-{int(time.time())}", "demo_mode": False},
)
project_id = project["project_id"]
runtime = request("GET", "/v1/runtime/status", token=token)
if runtime.get("local_fallback_active"):
    raise SystemExit(f"Runtime status reports local_fallback_active=true: {runtime}")
if frontend_url:
    request("GET", frontend_url)
with open(work_file, "w", encoding="utf-8") as handle:
    json.dump({"token": token, "project_id": project_id}, handle)
print("Core health/ready/capabilities/login/project/runtime checks passed.")
PY

TOKEN="$(python3 - "$WORK_FILE" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["token"])
PY
)"
PROJECT_ID="$(python3 - "$WORK_FILE" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["project_id"])
PY
)"

echo "Configuring private Collector app for smoke project..."
az containerapp secret set \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$COLLECTOR_APP_NAME" \
  --secrets "incidentops-token=$TOKEN" \
  --only-show-errors >/dev/null

az containerapp update \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$COLLECTOR_APP_NAME" \
  --set-env-vars \
    INCIDENTOPS_TOKEN=secretref:incidentops-token \
    PROJECT_ID="$PROJECT_ID" \
    INCIDENTOPS_PROJECT_ID="$PROJECT_ID" \
  --min-replicas 1 \
  --only-show-errors >/dev/null

echo "Configuring private MCP app with the same Core access token..."
az containerapp secret set \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$MCP_APP_NAME" \
  --secrets "incidentops-mcp-token=$TOKEN" \
  --only-show-errors >/dev/null

az containerapp update \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$MCP_APP_NAME" \
  --set-env-vars \
    MCP_TOKEN=secretref:incidentops-mcp-token \
    INCIDENTOPS_MCP_TOKEN=secretref:incidentops-mcp-token \
    MCP_CORE_API_URL="$API_URL" \
  --min-replicas 1 \
  --only-show-errors >/dev/null

python3 - "$API_URL" "$WORK_FILE" "$SMOKE_QUERY" "$TIMEOUT_SECONDS" <<'PY'
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

api_url, work_file, query, timeout_seconds = sys.argv[1:5]
api_url = api_url.rstrip("/")
timeout = int(timeout_seconds)
state = json.load(open(work_file, encoding="utf-8"))
token = state["token"]
project_id = state["project_id"]
deadline = time.time() + timeout
latest_sync = None


def request(method: str, path: str, *, payload: dict | None = None, allow_404: bool = False):
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    url = f"{api_url}{path}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            if not body:
                return response.status, {}
            try:
                return response.status, json.loads(body)
            except json.JSONDecodeError:
                return response.status, {"_raw": body[:300]}
    except urllib.error.HTTPError as exc:
        if allow_404 and exc.code == 404:
            return exc.code, {}
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{method} {url} failed with HTTP {exc.code}: {body[:300]}") from exc


while time.time() < deadline:
    _, sources = request("GET", f"/v1/projects/{project_id}/sources")
    for source in sources:
        status, latest = request("GET", f"/v1/sources/{source['id']}/syncs/latest", allow_404=True)
        if status == 404:
            continue
        if latest.get("status") in {"success", "partial_success"}:
            latest_sync = latest
            break
    if latest_sync:
        break
    time.sleep(5)

if not latest_sync:
    raise SystemExit("Collector sync did not reach success/partial_success before timeout.")

_, search_payload = request(
    "POST",
    "/v1/search",
    payload={"project_id": project_id, "query": query, "top_k": 5},
)
if search_payload.get("total", 0) <= 0:
    raise SystemExit("Search returned zero evidence after collector sync.")

investigate_status, _ = request(
    "POST",
    "/v1/investigate",
    payload={"project_id": project_id, "query": query, "top_k": 5},
)
_, readiness = request("GET", f"/v1/projects/{project_id}/readiness")

print("Azure Smoke Summary")
print(f"  api_url: {api_url}")
print(f"  project_id: {project_id}")
print(f"  sync_status: {latest_sync.get('status')}")
print(f"  search_results: {search_payload.get('total', 0)}")
print(f"  top_evidence_paths: {[r.get('document_path') for r in search_payload.get('results', [])[:3]]}")
print(f"  investigation_status: {investigate_status}")
print(f"  readiness_score: {readiness.get('score')}")
PY
