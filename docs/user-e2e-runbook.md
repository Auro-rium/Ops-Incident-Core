# Cloud-Only User E2E Runbook

This runbook proves the real IncidentOps product path in Azure:

Frontend -> Core API -> Azure PostgreSQL/pgvector -> Redis worker queue -> Collector benchmark job -> Azure OpenAI -> retrieval/investigation -> Core MCP tools.

MCP is only an interface over Core. It does not ingest, normalize, or bypass Core auth/RBAC.

## Prerequisites

Required Azure resources:

- Azure Container Registry
- Azure Container Apps environment
- `incidentops-core-api`
- `incidentops-core-worker`
- `incidentops-collector`
- `incidentops-frontend`
- `incidentops-mcp`
- `incidentops-benchmark-job`
- migration job
- bootstrap admin job
- Azure PostgreSQL Flexible Server with pgvector
- Azure Cache for Redis
- Azure Key Vault
- Log Analytics
- Azure OpenAI / Foundry chat deployment
- Azure OpenAI / Foundry embedding deployment

Required local tooling for orchestration only:

```bash
az account show
az extension add --name containerapp --upgrade
```

Required environment variables:

```bash
export AZURE_RESOURCE_GROUP=incidentops-demo-swc-rg
export SMOKE_EMAIL=<bootstrap admin email>
export SMOKE_PASSWORD=<bootstrap admin password>
```

Do not print or commit secrets. The scripts use the password only to obtain a short-lived Core JWT and pass it to Container Apps as a secret.

## Runtime Status

The frontend Status page and the API endpoint show safe cloud runtime fields:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  https://<core-api>/v1/runtime/status
```

Expected production proof:

- `app_env=production`
- `llm_provider=azure_openai`
- `embedding_backend=azure_openai`
- `retrieval_backend=postgres_pgvector`
- `worker_mode=queue`
- `rate_limit_backend=redis`
- `mcp_enabled=true`
- `azure_openai_configured=true`
- `local_fallback_active=false`

The endpoint never returns API keys, connection strings, tokens, passwords, Key Vault secret names, or raw Azure OpenAI endpoints.

## Start the Benchmark Job

The benchmark job runs Collector in Azure against Temporal with a controlled scope:

- repo: `https://github.com/temporalio/temporal.git`
- max files: `1500`
- batch size: `100`
- included paths: `README.md`, `docs/`, `api/`, `proto/`, `schema/`, `service/`, `common/`, `temporal/`, `cmd/`, `config/`, `develop/`
- excluded paths: `.git/`, `.github/`, `temporaltest/`, `tools/`, `bin/`, `dist/`, `coverage/`

The recommended way is the full E2E script:

```bash
scripts/azure_user_e2e.sh
```

Manual job operations, if needed:

```bash
az containerapp job start \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name incidentops-benchmark-job

az containerapp job logs show \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name incidentops-benchmark-job \
  --tail 120
```

The job clones into container temp storage and prints a sanitized report summary to logs. It does not persist cloned repos or print tokens.

## Frontend User Demo

Open the frontend URL printed by `scripts/azure_user_e2e.sh`.

1. Status
   - Confirm Core health/ready.
   - Confirm runtime status shows Azure OpenAI, queue worker, Redis rate limiting, and MCP enabled.
   - Confirm `local_fallback_active=false`.

2. Readiness
   - Load the active project readiness report.
   - Show score, evidence coverage, missing evidence, weak question types, and suggested questions.

3. Search
   - Query: `Where is the history service implemented?`
   - Confirm evidence paths come from the Temporal repo.

4. Investigation
   - Query: `Which parts of the Temporal repo are relevant to investigating workflow task latency?`
   - Confirm cited evidence or an honest missing-evidence response.

## MCP Client Proof

The Core MCP server runs separately as `incidentops-mcp` and calls Core APIs with a protected token.

Smoke it from Azure:

```bash
PROJECT_ID=<project-id> scripts/azure_mcp_smoke.sh
```

Expected tool list:

- `get_capabilities`
- `get_readiness_report`
- `search_evidence`
- `investigate_incident`
- `get_sync_status`
- `get_latest_source_sync`
- `get_run_events`

If connecting an external MCP client, point it at the protected MCP transport/path configured for the Container App. For the current internal smoke, the script execs into the MCP Container App and uses `http://127.0.0.1:8080/mcp` so the MCP endpoint does not need to be public.

## One-Command User Proof

```bash
export AZURE_RESOURCE_GROUP=incidentops-demo-swc-rg
export SMOKE_EMAIL=<bootstrap admin email>
export SMOKE_PASSWORD=<bootstrap admin password>
scripts/azure_user_e2e.sh
```

The script prints:

- frontend URL
- Core API URL
- runtime provider/backend status
- benchmark job status
- files seen
- documents synced
- chunks created
- sync status
- readiness score
- search result count
- citation count
- search/investigation latency
- MCP smoke summary

Token usage is printed only if Core exposes provider token metrics. Otherwise it is reported as unavailable rather than guessed.

## Interpreting Results

Success means:

- No local Core/Postgres/Redis/Collector service is used.
- Core readiness passes in Azure.
- Runtime status proves Azure OpenAI/cloud mode.
- Collector benchmark job ingests Temporal in Azure.
- Search returns Temporal evidence.
- Investigation returns citations or an explicit insufficient-evidence response.
- MCP tools call Core successfully.

A weak readiness score is not automatically a failure. Temporal is a source repo, not a full company incident bundle, so missing logs/deploys/runbooks/incidents should be reported honestly.

## Cost and Teardown

Container Apps, PostgreSQL, Redis, ACR, Log Analytics, and Azure OpenAI can continue billing while deployed.

Scale down nonessential apps:

```bash
az containerapp update --resource-group "$AZURE_RESOURCE_GROUP" --name incidentops-collector --min-replicas 0
az containerapp update --resource-group "$AZURE_RESOURCE_GROUP" --name incidentops-mcp --min-replicas 0
```

Full teardown:

```bash
CONFIRM=delete-$AZURE_RESOURCE_GROUP scripts/azure_teardown.sh
```

Review `docs/azure-cost-guardrails.md` before leaving the environment running.
