# IncidentOps Core

IncidentOps Core is the backend for an applied-AI incident investigation product. It receives normalized engineering evidence from the Collector, indexes it in Postgres/pgvector, and exposes cited search, investigation, readiness, workflow, eval, metrics, and MCP interfaces.

The active deployment target is **Azure only**.

## System Repos

```text
Ops-Incident-Core        FastAPI API, worker, MCP server, retrieval, investigation
Ops-Incident-Collector   deterministic source inspection, redaction, normalization, Core sync
Ops-Incident-frontend    operator console
```

## Production Path

```text
Collector
  -> normalized document batches
  -> Core API
  -> Postgres + pgvector
  -> Redis-backed worker/runtime queue
  -> search / investigate / readiness / workflow
  -> MCP tools over Core APIs
```

Core does not own source-folder crawling in production. Local folder ingest remains a disabled-by-default compatibility path for tests and controlled development only.

## Azure Deployment

Azure deployment assets live under:

- `infra/azure/`
- `.github/workflows/deploy-azure.yml`
- `docs/azure-deployment.md`
- `docs/azure-cost-guardrails.md`

Azure services used:

- Azure Container Registry
- Azure Container Apps for API, worker, MCP server, Collector, frontend, migration job, bootstrap job
- Azure Database for PostgreSQL Flexible Server with pgvector enabled by migration
- Azure Cache for Redis
- Azure Key Vault
- Azure Monitor / Log Analytics

Services intentionally not used:

- AKS
- NAT Gateway
- multi-region deployment

## Azure OpenAI / Foundry

Production and staging require Azure OpenAI / Foundry configuration:

```text
REQUIRE_AZURE_OPENAI=true
AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_API_VERSION=2024-10-21
AZURE_OPENAI_CHAT_DEPLOYMENT=...
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=...
EMBEDDING_MODEL=azure-openai
EMBEDDING_DIM=384
```

The database vector column is currently `Vector(384)`, so the Azure embedding deployment must support the requested `dimensions=384` parameter.

Unit and integration tests still use deterministic local-hash embeddings so CI does not require paid model credentials.

## MCP Server

Core includes a real MCP server process:

```bash
python -m incidentops.mcp.server
```

Production Azure runs it as a separate Container App using streamable HTTP transport. The MCP server is an interface layer only. It does not ingest data, normalize files, bypass Core auth/RBAC, or diagnose locally. It delegates to Core APIs using a scoped Core access token.

Supported MCP tools:

- `get_capabilities`
- `get_readiness_report`
- `search_evidence`
- `investigate_incident`
- `get_sync_status`
- `get_latest_source_sync`
- `get_run_events`

Required MCP settings:

```text
MCP_CORE_API_URL=https://YOUR-CORE-API
MCP_TOKEN=<Core access token>
MCP_TRANSPORT=streamable-http
MCP_HOST=0.0.0.0
MCP_PORT=8080
MCP_PATH=/mcp
```

## Core API Capabilities

- JWT auth and project-scoped RBAC
- source registry
- collector registration
- sync lifecycle
- normalized batch ingestion
- idempotent indexing by `external_id` and `content_hash`
- hybrid retrieval
- cited answers
- incident investigation
- readiness report
- deterministic workflow runs and approvals
- eval runs
- audit events
- Prometheus-style metrics
- OpenTelemetry hooks

## Required Production Settings

```text
APP_ENV=production
DB_CREATE_ALL=false
DB_REQUIRE_MIGRATIONS=true
WORKER_MODE=queue
JOB_QUEUE_BACKEND=redis
RATE_LIMIT_BACKEND=redis
METRICS_BACKEND=prometheus
METRICS_PUBLIC=false
LOCAL_INGEST_ENABLED=false
ENABLE_LOCAL_INGEST=false
ALLOW_LOCAL_SEED_ADMIN=false
ALLOW_DEMO_PROJECT_BYPASS=false
DEMO_MODE_PUBLIC=false
ALLOW_WILDCARD_CORS=false
```

Production must run Alembic migrations before API startup. The API must not use SQLAlchemy `create_all`.

## Azure Commands

```bash
scripts/azure_login_check.sh
scripts/azure_build_push_images.sh
scripts/azure_deploy.sh
scripts/azure_run_migrations.sh
scripts/azure_bootstrap_admin.sh
scripts/azure_smoke.sh
```

Teardown:

```bash
CONFIRM=delete-$AZURE_RESOURCE_GROUP scripts/azure_teardown.sh
```

## Local Test Commands

Local commands are for development and CI verification, not production deployment:

```bash
uv run --extra dev ruff check .
uv run --extra dev python -m pytest tests/unit tests/integration -q
uv run --extra dev python scripts/check_migrations.py
```

## Documentation

- [Azure deployment](docs/azure-deployment.md)
- [Azure cost guardrails](docs/azure-cost-guardrails.md)
- [Deployment operations](docs/deployment.md)
- [Collector/Core contract](docs/collector-core-contract.md)
- [Security](docs/security.md)
- [Retrieval](docs/retrieval.md)
- [Agent workflow](docs/agent_workflow.md)
- [Evals](docs/evals.md)
