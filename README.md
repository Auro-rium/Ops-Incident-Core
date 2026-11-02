# IncidentOps Core

IncidentOps is an engineering-evidence backend for incident investigation. It
ingests repository and operational evidence through a deterministic Collector,
indexes it with Core, and returns project-scoped search and investigation
results with citations and missing-evidence warnings.

> Version context: This repository is the active V2 rewrite. The current cloud
> proof path is Azure Container Apps plus Azure PostgreSQL/Redis, Key Vault,
> Log Analytics, NVIDIA NIM-compatible models, and an explicitly external managed
> Qdrant cluster. The Vercel browser console is separate and currently paused.

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
  F[apps/web on Vercel] -->|authenticated browser HTTP| API[FastAPI Core API]
  C[In-repository Collector] -->|redacted NormalizedDocument batches| API
  API --> PG[(PostgreSQL metadata and audit)]
  API --> Q[(Qdrant vectors)]
  API --> R[(Redis queue and limits)]
  R --> W[Core worker]
  W --> PG
  W --> Q
  API --> RET[Intent routing and hybrid retrieval]
  RET --> E[Compact cited evidence]
  E --> A[Direct answer or optional LLM synthesis]
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
apps/web/                 Vercel-hosted browser console for a Core API
incidentops/              backend domain code
incidentops/collector/    in-repository Collector runtime
incidentops/eval/         active eval runner and golden cases
incidentops/mcp/          Core MCP server
alembic/versions/v2/      V2 schema baseline and forward migrations
docker/core.Dockerfile    Core API/worker image
infra/azure/              Azure Bicep scaffold
scripts/                  migration, smoke, and Azure operations
tests/                    unit and integration tests
~~~

`apps/web` is a small public browser console. It does not proxy requests or
hold backend credentials: a user enters a Core API URL, signs in, and the
browser calls Core directly. The token is held only in browser session storage.
For a live connection, the Core deployment must allow the Vercel origin in
`CORS_ORIGINS` and expose its API over HTTPS.

Verified public console: https://web-auroriumnexus-6067s-projects.vercel.app

The deployed Core API is currently exposed at:
`https://incidentops-core-api.wittydesert-436ece0e.swedencentral.azurecontainerapps.io`

The API and console are separate origins. Configure the console origin in
Core's `CORS_ORIGINS`; do not put service credentials in the browser.

The Vercel project is linked to `Auro-rium/Ops-Incident-Core`, with `apps/web`
as its root directory and `core` as its production branch. A push to `core`
rebuilds the console. The current Azure deployment is verified separately by
the manual `.github/workflows/deploy-azure.yml` workflow.

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
11. When the Collector reports a complete sync that finished as `success`, Core
    deletes the source's documents that sync did not see, with their chunks,
    relations, and Qdrant points. A narrowed or degraded run never prunes.

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

Query analysis is deliberately conservative. Services are matched only against
names already indexed for the project. A deploy hash found in free text boosts
matching evidence; it becomes a hard filter only when the caller passes
`filters.deploy_hash`. Evidence containing instruction-like text is kept but
moved down the ranking and labelled `prompt_injection_flagged` in answer
responses.

Direct code/config/API lookups can use a fast cited-evidence path. Their
confidence is high only when the top hit clearly beats the runner-up. Optional
model synthesis is bounded to selected evidence and cites only the labels the
model reports using. A failed, non-JSON, or empty model reply returns
`retrieval_only` mode with an explicit warning. Local-hash embeddings remain
available for deterministic tests. The current production path uses the
NVIDIA NIM-compatible API for both chat and embeddings: Nemotron chat
(`nvidia/nemotron-3-super-120b-a12b`) and Nemotron embeddings
(`nvidia/nemotron-3-embed-1b`). Azure OpenAI remains an explicitly supported
alternative provider, but is not selected by the current production
configuration. Hugging Face is retained only as a compatibility fallback.

## API Surface

Common authenticated endpoints:

| Purpose | Endpoint |
|---|---|
| Login | POST /v1/auth/login |
| Revoke all of the caller's tokens | POST /v1/auth/logout-all |
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
| Resolve a pending approval | POST /v1/runs/{run_id}/approve, POST /v1/runs/{run_id}/reject |
| Evaluations | POST /v1/evals/run |
| Source purge | DELETE /v1/projects/{project_id}/sources/{source_id} |
| Project purge | DELETE /v1/projects/{project_id} |

/health is lightweight. /ready checks database, vector store, required
tables/columns, and the Alembic revision.

Approving or rejecting a run records the decision and marks its IssueDraft
approved or rejected. **It makes no GitHub API call and files no issue**; the
draft stays in Core for a person to act on. Resolving a run that is not awaiting
approval returns 409.

Evaluations are scored by threshold. A case passes only when it ran without
error, its evidence recall and term coverage (when it lists expected terms)
meet their minimums, and its forbidden-term hits stay within the limit.

## Database

The V2 schema is an immutable baseline plus forward migrations:

~~~text
alembic/versions/v2/v2_baseline.py
alembic/versions/v2/v2_0002_review_fixes.py
~~~

The baseline creates the original fresh-install schema; schema changes are
added as new migrations rather than edits to it.
incidentops/db/models.py remains the ORM model source; the Alembic files are the
database contract. Production startup does not call SQLAlchemy create_all.

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

The Core Compose file does not start the public frontend. Verify and deploy the
Vercel client separately:

~~~bash
cd apps/web
npm ci
npm run typecheck
npm run build
vercel --prod
~~~

Set `NEXT_PUBLIC_CORE_API_URL` in Vercel only when a public Core API exists.
Without it, a user can enter the API URL in the console. Never put API keys,
database URLs, or service credentials in Vercel environment variables.

~~~bash
uv run --extra dev ruff check .
uv run --extra dev python -m pytest tests/unit tests/integration -q
uv run python -m compileall incidentops apps scripts
docker compose config --quiet
~~~

## Azure State

Azure is the only retained cloud deployment direction in this repository. The
live resource group is `incidentops-demo-swc-rg` in `swedencentral` and the
deployed path includes Core API, Core worker, private MCP, Collector, Azure
PostgreSQL Flexible Server, Redis, Key Vault, ACR, Container Apps jobs, and Log
Analytics. The legacy frontend Container App remains disabled; Vercel hosts
`apps/web`.

The current API health and readiness checks have passed against the deployed
Core URL. Production settings require queue mode, Redis rate limiting, NVIDIA
Nemotron cloud chat and embeddings, and no local fallback. The
Nemotron index uses 2048-dimensional vectors in a versioned Qdrant collection;
existing vectors from another embedding provider must be reingested.
The private MCP
probe is opt-in in CI and runs from inside Azure rather than exposing MCP
publicly.

Qdrant is a managed external dependency configured through Key Vault. This is
Azure-first, not single-cloud: migrating vector storage to Azure AI Search or
PostgreSQL/pgvector is a separate measured project and is intentionally not
claimed here.

The manual workflow is `.github/workflows/deploy-azure.yml`. It tests Core and
`apps/web`, validates the Bicep entrypoint, builds/pushes the image, deploys
backend resources, runs migrations/bootstrap, and runs Azure smoke. Its
optional `run_mcp_smoke` input starts a short-lived private MCP probe job.
Set the public Vercel URL as `VERCEL_FRONTEND_URL` and include it in
`CORS_ORIGINS` before deployment.

## Security

- JWT authentication and bcrypt password hashing.
- Accounts lock temporarily after repeated failed logins
  (`LOGIN_MAX_FAILED_ATTEMPTS`, `LOGIN_LOCKOUT_MINUTES`).
- `POST /v1/auth/logout-all` revokes every outstanding token for the caller.
- Project-scoped RBAC for API operations.
- Collector path policy and pre-ingest redaction.
- No secrets in audit events or retrieval diagnostics.
- Local ingest disabled in production-like settings.
- Metrics private by default.
- MCP delegates to Core and cannot access the database directly.
- HTTP MCP transports require `Authorization: Bearer <MCP_INBOUND_TOKEN>` and
  refuse to start without that token.
- Internal PostgreSQL, Redis, Qdrant, Collector, worker, and MCP services must
  not be publicly exposed.

Deploying needs a `MCP_INBOUND_TOKEN` secret. `METRICS_TOKEN` is an optional
secret. `TRUSTED_PROXIES` and `ENABLE_PRIVATE_NETWORKING` are optional variables;
private networking is off by default and needs a fresh resource group.

## Honest Limitations

This is not yet production-grade. Remaining proof includes:

- worker and indexing failure/restart testing;
- measured retrieval quality and latency on multiple repositories;
- backup/restore and purge/reingestion drills;
- model token/latency measurements from the active NVIDIA chat path.

Known limits of the current behaviour:

- Stale-document removal needs a full, clean Collector sync. A run using
  `--max-files` or path filters, or one with rejected documents, read failures,
  or failed index jobs, leaves stale documents in place until the next clean run.
  The reason is recorded in the sync's `diagnostics.stale_prune_skipped`.
- Approval marks the IssueDraft only. Nothing is filed in GitHub.
- `MCP_INBOUND_TOKEN` is one shared static bearer token: it does not identify the
  caller, and outbound calls to Core use the service `MCP_TOKEN`, so Core sees the
  MCP service rather than the end user. The stdio transport has no inbound auth.
- Reindex re-embeds from content Core already stores, so on the production
  configuration it calls the paid NVIDIA embeddings API. It does not re-read the
  source; picking up source edits needs a Collector sync.
- Prompt-injection handling only flags, demotes, and labels. Flagged text can
  still reach the model and direct answers, and detection is pattern-based.
- Evals measure retrieval against golden cases with default thresholds. They do
  not judge answer quality or replace real incident benchmarks.
- Account lockout is per account, so repeated failures can lock out a legitimate
  user for the lockout window.
- Network hardening (VNet and private endpoints) is opt-in and has not been
  deployed.
- The Vercel console is paused; Core changes are not exercised through it.
