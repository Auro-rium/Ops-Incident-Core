#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

AZURE_RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-incidentops-demo-swc-rg}"
COLLECTOR_APP_NAME="${COLLECTOR_APP_NAME:-incidentops-collector}"
MCP_APP_NAME="${MCP_APP_NAME:-incidentops-mcp}"
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
    --name incidentops-core-api \
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

import httpx

api_url, frontend_url, email, password, work_file = sys.argv[1:6]
with httpx.Client(base_url=api_url.rstrip("/"), timeout=30, verify=True) as client:
    health = client.get("/health")
    health.raise_for_status()
    ready = client.get("/ready")
    ready.raise_for_status()
    caps = client.get("/v1/capabilities")
    caps.raise_for_status()
    login = client.post("/v1/auth/login", json={"email": email, "password": password})
    login.raise_for_status()
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    project = client.post(
        "/v1/projects",
        headers=headers,
        json={"name": f"azure-smoke-{int(time.time())}", "demo_mode": False},
    )
    project.raise_for_status()
    project_id = project.json()["project_id"]
    if frontend_url:
        front = httpx.get(frontend_url, timeout=30, verify=True)
        front.raise_for_status()
    with open(work_file, "w", encoding="utf-8") as handle:
        json.dump({"token": token, "project_id": project_id}, handle)
print("Core health/ready/capabilities/login/project checks passed.")
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

import httpx

api_url, work_file, query, timeout_seconds = sys.argv[1:5]
timeout = int(timeout_seconds)
state = json.load(open(work_file, encoding="utf-8"))
headers = {"Authorization": f"Bearer {state['token']}"}
project_id = state["project_id"]
deadline = time.time() + timeout
latest_sync = None

with httpx.Client(base_url=api_url.rstrip("/"), timeout=30, verify=True) as client:
    while time.time() < deadline:
        sources = client.get(f"/v1/projects/{project_id}/sources", headers=headers)
        sources.raise_for_status()
        for source in sources.json():
            latest_response = client.get(f"/v1/sources/{source['id']}/syncs/latest", headers=headers)
            if latest_response.status_code == 404:
                continue
            latest_response.raise_for_status()
            latest = latest_response.json()
            if latest.get("status") in {"success", "partial_success"}:
                latest_sync = latest
                break
        if latest_sync:
            break
        time.sleep(5)

    if not latest_sync:
        raise SystemExit("Collector sync did not reach success/partial_success before timeout.")

    search = client.post(
        "/v1/search",
        headers=headers,
        json={"project_id": project_id, "query": query, "top_k": 5},
    )
    search.raise_for_status()
    search_payload = search.json()
    if search_payload.get("total", 0) <= 0:
        raise SystemExit("Search returned zero evidence after collector sync.")

    investigate = client.post(
        "/v1/investigate",
        headers=headers,
        json={"project_id": project_id, "query": query, "top_k": 5},
    )
    investigate.raise_for_status()
    readiness = client.get(f"/v1/projects/{project_id}/readiness", headers=headers)
    readiness.raise_for_status()

    print("Azure Smoke Summary")
    print(f"  api_url: {api_url}")
    print(f"  project_id: {project_id}")
    print(f"  sync_status: {latest_sync.get('status')}")
    print(f"  search_results: {search_payload.get('total', 0)}")
    print(f"  top_evidence_paths: {[r.get('document_path') for r in search_payload.get('results', [])[:3]]}")
    print(f"  investigation_status: {investigate.status_code}")
    print(f"  readiness_score: {readiness.json().get('score')}")
PY
