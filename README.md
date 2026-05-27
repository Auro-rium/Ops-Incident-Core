# IncidentOps Core

IncidentOps Core is the Azure-first backend for an applied-AI incident investigation product. It receives normalized engineering evidence from the Collector, indexes it in PostgreSQL/pgvector, and exposes readiness, cited search, investigation, workflow, eval, metrics, and MCP interfaces.

The point is not to be another PDF chatbot with a trench coat. The point is to help engineering teams turn messy repos, logs, docs, deploy metadata, runbooks, and incident notes into inspectable evidence that can support incident triage.

## Current Product Position

IncidentOps is now an **Azure-only production-style path**:

```text
Repo / docs / logs bundle
  -> Ops-Incident-Collector
  -> deterministic scan, redaction, metadata, normalization
  -> Core batch ingest API
  -> PostgreSQL documents/chunks + pgvector
  -> Azure OpenAI embeddings and answer synthesis
  -> readiness / search / investigate / workflow
  -> Core MCP tools for external AI clients
  -> frontend operator console
```

Local execution remains only for deterministic development and CI safety. Production/staging must not depend on local hash embeddings, inline workers, local ingest, or mock model paths.

## System Repositories

```text
Ops-Incident-Core        FastAPI API, worker, MCP server, retrieval, investigation, readiness
Ops-Incident-Collector   deterministic source inspection, redaction, normalization, Core sync
Ops-Incident-frontend    operator console
```

Core owns indexing, retrieval, readiness, investigation, workflow, auth/RBAC, metrics, audit, evals, and MCP over Core APIs. Collector owns source access, path policy, redaction, metadata extraction, and `NormalizedDocument` sync. Frontend owns the human operator experience.

## What Is Proven

The Azure path has been wired for real deployment through GitHub Actions and Azure Container Apps:

- Core push runs tests, builds Core/Collector/frontend images, deploys Azure Container Apps, runs migrations, bootstraps admin, and runs Azure smoke.
- Frontend and Collector repository workflows can dispatch the Core Azure deployment path.
- Azure smoke validates Core health/readiness, Collector sync, search, investigate, and readiness.
- Runtime status should show production/staging using Azure OpenAI, Redis queue/rate limit, and no local fallback.

The project has also run a real cloud E2E with Azure OpenAI embeddings/synthesis, PostgreSQL/pgvector retrieval, frontend access, Collector ingestion, and internal Core MCP deployment. The strongest next proof is deeper Temporal-scale code intelligence, not more cloud plumbing. Humanity may recover from that restraint.

## Current Bottleneck

The first Temporal stress run proved the cloud system worked, but also exposed the repo-intelligence bottleneck:

```text
Temporal benchmark snapshot:
files_seen: 1500
files_skipped: 1413
documents_normalized: 87
documents_received_by_core: 87
chunks_created: 12
parser_errors / skipped_invalid: 85
sync_status: partial_success
```

The issue was coverage quality, not the cloud loop. Temporal is mostly Go/proto, so the next useful engineering milestone is Go/proto ingestion, parsing, chunking, and retrieval validation at scale.

See [Temporal benchmark](docs/temporal-benchmark.md) and [Product proof](docs/product-proof.md).

## Azure Deployment

Azure deployment assets live under:

- `infra/azure/`
- `.github/workflows/deploy-azure.yml`
- `docs/azure-deployment.md`
- `docs/azure-cost-guardrails.md`

Azure services used:

- Azure Container Registry
- Azure Container Apps for API, worker, MCP server, Collector, frontend, migration job, and bootstrap job
- Azure Database for PostgreSQL Flexible Server with pgvector enabled by migration
- Azure Cache for Redis
- Azure Key Vault
- Azure Monitor / Log Analytics
- Azure OpenAI / Foundry for production/staging chat and embeddings

Services intentionally not used in the budget-demo architecture:

- AKS
- NAT Gateway
- multi-region deployment
- API Management / Application Gateway
- Azure AI Search, until the managed retrieval backend is explicitly implemented and benchmarked

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

Unit and integration tests still use deterministic local-hash embeddings so CI does not require paid model credentials. That is test discipline, not a production fallback.

## MCP Boundary

Core MCP is the product MCP. Collector MCP, if used, is local/private operator tooling only.

Core MCP tools:

- `get_capabilities`
- `get_readiness_report`
- `search_evidence`
- `investigate_incident`
- `get_sync_status`
- `get_latest_source_sync`
- `get_run_events`

The MCP server delegates to Core APIs using a scoped token. It must not ingest data, normalize files, access the database directly, bypass Core RBAC, or diagnose locally.

See [MCP architecture](docs/mcp-architecture.md).

## Core API Capabilities

- JWT auth and project-scoped RBAC
- source registry and collector registration
- sync lifecycle and normalized batch ingestion
- idempotent indexing by `external_id` and `content_hash`
- source-aware parsing/chunking for supported code/docs/log/config/deploy/incident evidence
- PostgreSQL full-text and pgvector retrieval
- Azure OpenAI-backed embeddings and answer synthesis in production/staging
- cited search and cited answer generation
- incident investigation with missing-data warnings
- readiness reports
- deterministic workflow runs and approvals
- eval runs
- audit events
- Prometheus-style metrics and OpenTelemetry hooks
- Core MCP server over Core APIs

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
REQUIRE_AZURE_OPENAI=true
EMBEDDING_MODEL=azure-openai
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

- [Architecture](docs/architecture.md)
- [Azure deployment](docs/azure-deployment.md)
- [Azure cost guardrails](docs/azure-cost-guardrails.md)
- [Cloud-only user E2E runbook](docs/user-e2e-runbook.md)
- [Product proof](docs/product-proof.md)
- [Temporal benchmark](docs/temporal-benchmark.md)
- [MCP architecture](docs/mcp-architecture.md)
- [Collector/Core contract](docs/collector-core-contract.md)
- [Security](docs/security.md)
