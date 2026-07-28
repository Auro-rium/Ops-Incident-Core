#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AZURE_RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
NAME_PREFIX="${NAME_PREFIX:?NAME_PREFIX is required}"
CORE_APP_NAME="${CORE_APP_NAME:-${NAME_PREFIX}-core-api}"
FRONTEND_APP_NAME="${FRONTEND_APP_NAME:-${NAME_PREFIX}-frontend}"
BENCHMARK_JOB_NAME="${BENCHMARK_JOB_NAME:-${NAME_PREFIX}-benchmark-job}"
MCP_APP_NAME="${MCP_APP_NAME:-${NAME_PREFIX}-mcp}"
SMOKE_EMAIL="${SMOKE_EMAIL:-${BOOTSTRAP_ADMIN_EMAIL:-admin@incidentops.local}}"
SMOKE_PASSWORD="${SMOKE_PASSWORD:-${BOOTSTRAP_ADMIN_PASSWORD:-}}"
PROJECT_ID="${PROJECT_ID:-${INCIDENTOPS_PROJECT_ID:-}}"
PROJECT_NAME="${PROJECT_NAME:-azure-user-e2e-temporal}"
REPO_URL="${REPO_URL:-https://github.com/temporalio/temporal.git}"
SOURCE_NAME="${SOURCE_NAME:-temporal}"
MAX_FILES="${MAX_FILES:-1500}"
BATCH_SIZE="${BATCH_SIZE:-100}"
CHANGED_FILE_TARGET="${CHANGED_FILE_TARGET:-README.md}"
SEARCH_QUERY="${SEARCH_QUERY:-Where is the history service implemented?}"
INVESTIGATE_QUERY="${INVESTIGATE_QUERY:-Which parts of the Temporal repo are relevant to investigating workflow task latency?}"
WAIT_TIMEOUT_SECONDS="${WAIT_TIMEOUT_SECONDS:-7200}"
POLL_SECONDS="${POLL_SECONDS:-20}"
STATE_FILE="$(mktemp)"
trap 'rm -f "$STATE_FILE"' EXIT

"$ROOT_DIR/scripts/azure_login_check.sh"

if [[ -z "$SMOKE_PASSWORD" ]]; then
  echo "SMOKE_PASSWORD or BOOTSTRAP_ADMIN_PASSWORD is required." >&2
  exit 1
fi

api_fqdn="$(az containerapp show --resource-group "$AZURE_RESOURCE_GROUP" --name "$CORE_APP_NAME" --query 'properties.configuration.ingress.fqdn' --output tsv --only-show-errors)"
frontend_fqdn="$(az containerapp show --resource-group "$AZURE_RESOURCE_GROUP" --name "$FRONTEND_APP_NAME" --query 'properties.configuration.ingress.fqdn' --output tsv --only-show-errors)"
API_URL="${API_URL:-https://$api_fqdn}"
FRONTEND_URL="${FRONTEND_URL:-https://$frontend_fqdn}"

echo "Frontend URL: $FRONTEND_URL"
echo "Core API URL: $API_URL"

python3 - "$API_URL" "$FRONTEND_URL" "$SMOKE_EMAIL" "$SMOKE_PASSWORD" "$PROJECT_ID" "$PROJECT_NAME" "$SEARCH_QUERY" "$INVESTIGATE_QUERY" "$STATE_FILE" <<'PY'
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

api_url, frontend_url, email, password, project_id, project_name, search_query, investigate_query, state_file = sys.argv[1:10]
api_url = api_url.rstrip('/')


def request(method: str, path: str, *, token: str | None = None, payload: dict | None = None, allow_error: bool = False):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(api_url + path, data=data, headers=headers, method=method)
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            body = response.read().decode('utf-8')
            latency_ms = int((time.time() - start) * 1000)
            payload = json.loads(body) if body else {}
            return response.status, payload, latency_ms
    except urllib.error.HTTPError as exc:
        if allow_error:
            return exc.code, {'error': exc.read().decode('utf-8', errors='replace')[:500]}, int((time.time() - start) * 1000)
        body = exc.read().decode('utf-8', errors='replace')
        raise SystemExit(f'{method} {path} failed HTTP {exc.code}: {body[:500]}') from exc

health = request('GET', '/health')[1]
ready = request('GET', '/ready')[1]
capabilities = request('GET', '/v1/capabilities')[1]
frontend_request = urllib.request.Request(frontend_url)
with urllib.request.urlopen(frontend_request, timeout=30) as response:
    frontend_page = response.read().decode('utf-8', errors='replace')
if 'IncidentOps Console' not in frontend_page:
    raise SystemExit('Frontend did not return the IncidentOps Console shell.')
login = request('POST', '/v1/auth/login', payload={'email': email, 'password': password})[1]
token = login['access_token']
if not project_id:
    project = request('POST', '/v1/projects', token=token, payload={'name': f'{project_name}-{int(time.time())}', 'demo_mode': False})[1]
    project_id = project['project_id']
runtime = request('GET', '/v1/runtime/status', token=token)[1]
if runtime.get('local_fallback_active'):
    raise SystemExit(f"Runtime reports local_fallback_active=true: {runtime}")
state = {
    'api_url': api_url,
    'token': token,
    'project_id': project_id,
    'health': health,
    'ready': ready,
    'capabilities': capabilities,
    'runtime': runtime,
    'search_query': search_query,
    'investigate_query': investigate_query,
}
with open(state_file, 'w', encoding='utf-8') as handle:
    json.dump(state, handle)
print('Frontend/Core health/ready/capabilities/login/runtime checks passed.')
print(f"project_id: {project_id}")
print(f"runtime: provider={runtime.get('llm_provider')} embedding={runtime.get('embedding_backend')} retrieval={runtime.get('retrieval_backend')} worker={runtime.get('worker_mode')}")
PY

TOKEN="$(python3 - "$STATE_FILE" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding='utf-8'))['token'])
PY
)"
PROJECT_ID="$(python3 - "$STATE_FILE" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding='utf-8'))['project_id'])
PY
)"

if az containerapp job show --resource-group "$AZURE_RESOURCE_GROUP" --name "$BENCHMARK_JOB_NAME" --only-show-errors >/dev/null 2>&1; then
  echo "Configuring benchmark job '$BENCHMARK_JOB_NAME' for project $PROJECT_ID..."
  az containerapp job secret set \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$BENCHMARK_JOB_NAME" \
    --secrets "incidentops-token=$TOKEN" \
    --only-show-errors >/dev/null
  az containerapp job update \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$BENCHMARK_JOB_NAME" \
    --set-env-vars \
      INCIDENTOPS_API_URL="$API_URL" \
      INCIDENTOPS_TOKEN=secretref:incidentops-token \
      INCIDENTOPS_PROJECT_ID="$PROJECT_ID" \
      REPO_URL="$REPO_URL" \
      SOURCE_NAME="$SOURCE_NAME" \
      MAX_FILES="$MAX_FILES" \
      BATCH_SIZE="$BATCH_SIZE" \
      CHANGED_FILE_TARGET="$CHANGED_FILE_TARGET" \
      QUERY_1="$SEARCH_QUERY" \
      QUERY_2="$INVESTIGATE_QUERY" \
    --only-show-errors >/dev/null

  echo "Starting benchmark job '$BENCHMARK_JOB_NAME'..."
  execution_name="$(az containerapp job start --resource-group "$AZURE_RESOURCE_GROUP" --name "$BENCHMARK_JOB_NAME" --query name --output tsv --only-show-errors 2>/dev/null || true)"
  deadline=$((SECONDS + WAIT_TIMEOUT_SECONDS))
  status="unknown"
  while (( SECONDS < deadline )); do
    if [[ -n "$execution_name" ]]; then
      status="$(az containerapp job execution show --resource-group "$AZURE_RESOURCE_GROUP" --job-name "$BENCHMARK_JOB_NAME" --name "$execution_name" --query 'properties.status' --output tsv --only-show-errors 2>/dev/null || true)"
    else
      status="$(az containerapp job execution list --resource-group "$AZURE_RESOURCE_GROUP" --name "$BENCHMARK_JOB_NAME" --query '[0].properties.status' --output tsv --only-show-errors 2>/dev/null || true)"
    fi
    echo "benchmark_status: ${status:-pending}"
    case "$status" in
      Succeeded|Completed) break ;;
      Failed) echo "Benchmark job failed. Recent logs:" >&2; az containerapp job logs show --resource-group "$AZURE_RESOURCE_GROUP" --name "$BENCHMARK_JOB_NAME" --follow false --tail 120 --only-show-errors || true; exit 1 ;;
    esac
    sleep "$POLL_SECONDS"
  done
  if [[ "$status" != "Succeeded" && "$status" != "Completed" ]]; then
    echo "Benchmark job did not complete before timeout." >&2
    exit 1
  fi
  echo "Benchmark job completed. Recent logs:"
  az containerapp job logs show --resource-group "$AZURE_RESOURCE_GROUP" --name "$BENCHMARK_JOB_NAME" --follow false --tail 120 --only-show-errors || true
else
  echo "Benchmark job '$BENCHMARK_JOB_NAME' does not exist; skipping job start and checking existing Core evidence." >&2
fi

python3 - "$API_URL" "$STATE_FILE" <<'PY'
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

api_url, state_file = sys.argv[1:3]
api_url = api_url.rstrip('/')
state = json.load(open(state_file, encoding='utf-8'))
token = state['token']
project_id = state['project_id']
search_query = state['search_query']
investigate_query = state['investigate_query']
headers = {'Authorization': f'Bearer {token}'}


def request(method: str, path: str, *, payload: dict | None = None, allow_404: bool = False):
    data = None
    local_headers = dict(headers)
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        local_headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(api_url + path, data=data, headers=local_headers, method=method)
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            body = response.read().decode('utf-8')
            return response.status, json.loads(body) if body else {}, int((time.time() - start) * 1000)
    except urllib.error.HTTPError as exc:
        if allow_404 and exc.code == 404:
            return exc.code, {}, int((time.time() - start) * 1000)
        body = exc.read().decode('utf-8', errors='replace')
        raise SystemExit(f'{method} {path} failed HTTP {exc.code}: {body[:500]}') from exc

_, sources_payload, _ = request('GET', f'/v1/projects/{project_id}/sources')
sources = sources_payload if isinstance(sources_payload, list) else sources_payload.get('sources', [])
latest_sync = None
for source in sources:
    source_id = source.get('id') or source.get('source_id')
    if not source_id:
        continue
    status, payload, _ = request('GET', f'/v1/sources/{source_id}/syncs/latest', allow_404=True)
    if status == 404:
        continue
    latest_sync = payload
    break
_, readiness, readiness_latency = request('GET', f'/v1/projects/{project_id}/readiness')
_, search, search_latency = request('POST', '/v1/search', payload={'project_id': project_id, 'query': search_query, 'top_k': 5, 'debug': True})
_, answer, answer_latency = request('POST', '/v1/answer', payload={'project_id': project_id, 'query': search_query, 'top_k': 5})
_, investigation, investigation_latency = request('POST', '/v1/investigate', payload={'project_id': project_id, 'query': investigate_query, 'top_k': 5})
search_results = search.get('results', []) or []
if int(search.get('total', len(search_results)) or 0) <= 0:
    raise SystemExit('Search returned zero evidence for user E2E query.')
metrics = {
    'project_id': project_id,
    'source_count': len(sources),
    'latest_sync_status': latest_sync.get('status') if latest_sync else None,
    'files_seen': latest_sync.get('files_seen') if latest_sync else None,
    'docs_synced': latest_sync.get('documents_received') if latest_sync else None,
    'chunks_created': latest_sync.get('chunks_created') if latest_sync else None,
    'readiness_score': readiness.get('score'),
    'search_result_count': int(search.get('total', len(search_results)) or 0),
    'top_evidence_paths': [item.get('document_path') for item in search_results[:5]],
    'investigation_citation_count': len(investigation.get('citations') or investigation.get('evidence') or []),
    'search_latency_ms': search.get('latency_ms', search_latency),
    'answer_latency_ms': answer.get('latency_ms', answer_latency),
    'investigation_latency_ms': investigation.get('latency_ms', investigation_latency),
    'readiness_latency_ms': readiness_latency,
    'query_intent': search.get('query_intent'),
    'retrieval_branch_latencies': (search.get('debug') or {}).get('retrieval_branch_latencies', {}),
    'llm_usage': answer.get('llm_usage') or {},
    'llm_latency_ms': answer.get('llm_latency_ms'),
}
state['metrics'] = metrics
state['latest_sync'] = latest_sync
with open(state_file, 'w', encoding='utf-8') as handle:
    json.dump(state, handle)
print('Azure User E2E Core Metrics')
print(json.dumps(metrics, indent=2, sort_keys=True))
PY

PROJECT_ID="$PROJECT_ID" SEARCH_QUERY="$SEARCH_QUERY" INVESTIGATE_QUERY="$INVESTIGATE_QUERY" NAME_PREFIX="$NAME_PREFIX" MCP_APP_NAME="$MCP_APP_NAME" "$ROOT_DIR/scripts/azure_mcp_smoke.sh" | tee /tmp/incidentops-mcp-smoke.out

python3 - "$STATE_FILE" "$FRONTEND_URL" "$API_URL" <<'PY'
import json, sys
state = json.load(open(sys.argv[1], encoding='utf-8'))
frontend_url, api_url = sys.argv[2:4]
runtime = state.get('runtime', {})
metrics = state.get('metrics', {})
print('\nIncidentOps Cloud User E2E Summary')
print(f"frontend_url: {frontend_url}")
print(f"core_api_url: {api_url}")
print(f"project_id: {state.get('project_id')}")
print(f"model_provider: {runtime.get('llm_provider')}")
print(f"embedding_backend: {runtime.get('embedding_backend')}")
print(f"retrieval_backend: {runtime.get('retrieval_backend')}")
print(f"worker_mode: {runtime.get('worker_mode')}")
print(f"rate_limit_backend: {runtime.get('rate_limit_backend')}")
print(f"mcp_enabled: {runtime.get('mcp_enabled')}")
print(f"files_seen: {metrics.get('files_seen')}")
print(f"docs_synced: {metrics.get('docs_synced')}")
print(f"chunks_created: {metrics.get('chunks_created')}")
print(f"sync_status: {metrics.get('latest_sync_status')}")
print(f"readiness_score: {metrics.get('readiness_score')}")
print(f"search_result_count: {metrics.get('search_result_count')}")
print(f"investigation_citation_count: {metrics.get('investigation_citation_count')}")
print(f"search_latency_ms: {metrics.get('search_latency_ms')}")
print(f"answer_latency_ms: {metrics.get('answer_latency_ms')}")
print(f"investigation_latency_ms: {metrics.get('investigation_latency_ms')}")
print(f"query_intent: {metrics.get('query_intent')}")
print(f"retrieval_branch_latencies: {metrics.get('retrieval_branch_latencies')}")
print(f"token_usage: {metrics.get('llm_usage')}")
print(f"llm_latency_ms: {metrics.get('llm_latency_ms')}")
PY
