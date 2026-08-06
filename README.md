# IncidentOps Core

IncidentOps is an engineering-evidence backend for incident investigation. It
ingests repository and operational evidence through a deterministic Collector,
indexes it with Core, and returns project-scoped search and investigation
results with citations and missing-evidence warnings.

> Version context: This repository is the active V2 rewrite. It is not a
> production deployment. Azure model resources exist, but no Azure Container
> Apps, PostgreSQL, Redis, Qdrant, Collector, frontend, or MCP runtime is
> currently deployed in the inspected subscription.

## Product Boundary

The useful claim is narrow and testable:

- ingest code, documentation, configuration, API contracts, logs, deploy
  records, runbooks, and incident notes through normalized documents;
- retrieve evidence with citations and query-aware diagnostics;
- explain what evidence exists and what is missing;
- produce cautious answers instead of unsupported root-cause certainty.

A repository alone cannot explain a runtime outage. Runtime investigation needs
timestamped logs, deploy/change context, and often incident history.

## Architecture

~~~mermaid
flowchart LR
  F[Ops-Incident-frontend] -->|authenticated HTTP| API[FastAPI Core API]
  C[In-repository Collector] -->|redacted NormalizedDocument batches| API
  API --> PG[(PostgreSQL metadata and audit)]
  API --> Q[(Qdrant vectors)]
  API --> R[(Redis queue and limits)]
  R --> W[Core worker]
  W --> PG
  W --> Q
  API --> RET[Intent routing and hybrid retrieval]
  RET --> E[Compact cited evidence]
  E --> A[Direct answer or optional Azure OpenAI synthesis]
  M[Core MCP server] -->|Core API only| API
~~~

Core owns authentication, RBAC, project isolation, source/sync state,
normalization validation, chunking, indexing, retrieval, investigation,
workflow runs, evals, readiness, audit events, and MCP-facing tools.

Collector can read an approved source and submit normalized documents. It does
not access the database, normalize through an LLM, or diagnose incidents.
MCP is an interface over Core APIs; it is not an ingestion path and cannot
bypass Core authorization.

## Repository Layout

~~~text
apps/api/                 FastAPI composition and route registration
incidentops/              backend domain code
incidentops/collector/    in-repository Collector runtime
incidentops/eval/         active eval runner and golden cases
incidentops/mcp/          Core MCP server
alembic/versions/v2/      single clean-project V2 schema baseline
docker/core.Dockerfile    Core API/worker image
infra/azure/              Azure Bicep scaffold
scripts/                  migration, smoke, and Azure operations
tests/                    unit and integration tests
~~~

The frontend is intentionally separate:

~~~text
Ops-Incident-frontend
~~~

The old embedded apps/web frontend was removed. Core no longer builds or ships
an embedded frontend.

## Data Flow

1. A project and source are created through Core.
2. Collector registers and starts a sync.
3. Collector discovers allowed files and redacts likely credentials.
4. Collector sends authenticated NormalizedDocument batches.
5. Core validates limits and metadata, records typed failures, and skips
   unchanged content by hash.
6. Core creates source-aware chunks and stores authoritative text/metadata in
   PostgreSQL.
7. Qdrant stores vector points linked to authorized Core chunks.
8. Search combines vector, lexical, metadata, path, and graph evidence.
9. Investigation returns citations, confidence, and missing-data warnings.
10. Workers execute queued indexing, workflow, eval, observer, and logging jobs.

## Retrieval

Query intent classes include:

~~~text
code_location
architecture
config_lookup
api_contract
runtime_incident
deploy_regression
previous_incident
runbook_lookup
generic
~~~

Retrieval diagnostics expose intent, budget, source/chunk distributions,
branch latency, and applied ranking adjustments without exposing secrets or
large document bodies.

Direct code/config/API lookups can use a fast cited-evidence path. Optional
model synthesis is bounded to selected evidence. Local-hash embeddings remain
available for deterministic tests; Azure OpenAI is the intended cloud provider.

## API Surface

Common authenticated endpoints:

| Purpose | Endpoint |
|---|---|
| Login | POST /v1/auth/login |
| Capabilities | GET /v1/capabilities |
| Runtime status | GET /v1/runtime/status |
| Project readiness | GET /v1/projects/{project_id}/readiness |
| Register source | POST /v1/projects/{project_id}/sources |
| Register Collector | POST /v1/projects/{project_id}/collectors/register |
| Start sync | POST /v1/sources/{source_id}/syncs/start |
| Batch documents | POST /v1/sources/{source_id}/documents/batch |
| Finish sync | POST /v1/sources/{source_id}/syncs/{sync_id}/finish |
| Search | POST /v1/search |
| Answer | POST /v1/answer |
| Investigate | POST /v1/investigate |
| Workflow runs | POST /v1/runs |
| Evaluations | POST /v1/evals/run |
| Source purge | DELETE /v1/projects/{project_id}/sources/{source_id} |
| Project purge | DELETE /v1/projects/{project_id} |

/health is lightweight. /ready checks database, vector store, required
tables/columns, and the Alembic revision.

## Database

This clean V2 repository uses one Alembic baseline:

~~~text
alembic/versions/v2/v2_baseline.py
~~~

It creates the complete current schema in one fresh-install migration.
incidentops/db/models.py remains the ORM model source; the Alembic file is the
immutable database bootstrap contract. Production startup does not call
SQLAlchemy create_all.

~~~bash
alembic upgrade head
python scripts/check_migrations.py
~~~

## Local Verification

The Compose stack is for development and deterministic testing only:

~~~bash
docker compose up -d --build postgres qdrant redis
docker compose run --rm --no-deps api alembic upgrade head
docker compose up -d --build api core-worker
~~~

The Core Compose file no longer starts a frontend. Build and run
Ops-Incident-frontend separately when needed.

~~~bash
uv run --extra dev ruff check .
uv run --extra dev python -m pytest tests/unit tests/integration -q
uv run python -m compileall incidentops apps scripts
docker compose config --quiet
~~~

## Azure State

Azure is the only retained cloud deployment direction in this repository.
The Bicep scaffold describes Core API, worker, MCP, Collector, frontend,
PostgreSQL Flexible Server, Redis, Key Vault, ACR, Container Apps jobs, and
logging.

The inspected Azure subscription on 2026-08-07 contains:

| Resource group | Resource | Verified state |
|---|---|---|
| incident-ops | Cognitive Services account incident-ops | exists |
| incident-ops | Azure OpenAI incidentops-chat | gpt-5-mini, succeeded |
| incident-ops | Azure OpenAI incidentops-embed | text-embedding-3-small, succeeded |
| incidentops-demo-rg | ACR incidentops846e0b9 | exists; no repositories listed |

Microsoft.App is NotRegistered. No Container Apps environment, app, or job was
found. Therefore there is currently no public frontend URL, Core API URL,
Collector runtime, worker, MCP endpoint, or live cloud E2E result to report.

The Azure OpenAI deployments prove resource provisioning only. They do not
prove that an application call has executed.

The manual workflow is .github/workflows/deploy-azure.yml. It is a delivery
scaffold and requires Azure OIDC variables/secrets plus the separate frontend
repository. It has not been represented as a successful live deployment.

## Security

- JWT authentication and bcrypt password hashing.
- Project-scoped RBAC for API operations.
- Collector path policy and pre-ingest redaction.
- No secrets in audit events or retrieval diagnostics.
- Local ingest disabled in production-like settings.
- Metrics private by default.
- MCP delegates to Core and cannot access the database directly.
- Internal PostgreSQL, Redis, Qdrant, Collector, worker, and MCP services must
  not be publicly exposed.

## Honest Limitations

This is not yet production-grade. Missing proof includes:

- a clean Azure deployment;
- migration/bootstrap/smoke evidence on Azure;
- real cloud Collector sync after the V2 reset;
- worker and indexing failure/restart testing;
- measured retrieval quality and latency on multiple repositories;
- backup/restore and purge/reingestion drills;
- model token/latency measurements from real Azure OpenAI calls.
