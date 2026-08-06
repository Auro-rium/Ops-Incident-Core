# IncidentOps Core Technical Architecture

Status: V2 rewrite, active development, not production-grade.
Last verified: 2026-08-07.

This document describes the repository as it exists, not an aspirational
deployment.

## System Boundary

~~~mermaid
flowchart TB
  subgraph Client
    FE[apps/web on Vercel]
    MCP[MCP client]
  end
  subgraph Core[Ops-Incident-Core]
    API[FastAPI API: apps/api]
    COL[Collector: incidentops/collector]
    WORKER[Worker: incidentops.worker]
    MCP_SERVER[MCP: incidentops.mcp.server]
    DOMAIN[Auth, RBAC, ingest, retrieval, investigate, workflow, eval]
  end
  subgraph Data
    PG[(PostgreSQL)]
    QD[(Qdrant)]
    REDIS[(Redis)]
  end
  subgraph Models
    AZURE[Azure OpenAI chat and embeddings]
    REMOTE[Optional remote reranker endpoint]
    LOCAL[Deterministic local-hash test fallback]
  end
  FE --> API
  MCP --> MCP_SERVER
  MCP_SERVER --> API
  COL --> API
  API --> DOMAIN
  DOMAIN --> PG
  DOMAIN --> QD
  DOMAIN --> REDIS
  REDIS --> WORKER
  WORKER --> DOMAIN
  DOMAIN --> AZURE
  DOMAIN --> REMOTE
  DOMAIN --> LOCAL
~~~

## Components

### apps/api

The FastAPI composition layer. apps/api/main.py validates startup settings,
configures middleware, metrics, and tracing, registers routes, and prevents
create_all in production-like environments.

### incidentops

| Package | Responsibility |
|---|---|
| config | typed settings and startup validation |
| db | SQLAlchemy models, sessions, migration readiness |
| security | JWT, bcrypt, RBAC, rate limits, audit |
| collector | normalized document contract and in-repository Collector |
| ingest | validation and batch ingestion |
| retrieval | embeddings, Qdrant, lexical retrieval, fusion, reranking |
| investigation | evidence-backed investigation responses |
| agent | workflow graph, tools, approvals, events |
| worker | queue abstraction and job execution |
| eval | persisted evaluation runner and metrics |
| operations | observer, logging, operational runs/findings/events |
| readiness | deterministic coverage/readiness reports |
| mcp | Core API facade for external AI clients |

### Browser console

`apps/web` is a minimal Next.js browser console deployed independently to
Vercel. It communicates with Core over authenticated HTTPS, stores the user
access token only in browser session storage, and never receives database,
Qdrant, Redis, model, or service credentials. It is not a backend proxy.

The deployed Core API must include the Vercel origin in `CORS_ORIGINS`. The
console can be built without a configured API URL; users may enter one at run
time. `NEXT_PUBLIC_CORE_API_URL` may set a non-secret default only.

Verified public console: https://web-auroriumnexus-6067s-projects.vercel.app
The Vercel project is connected to `Auro-rium/Ops-Incident-Core`, uses
`apps/web` as its root directory, and has `core` as its production branch.
This deployment proves delivery of the browser UI, not a live backend path.

## Ingestion Contract

Collector sends authenticated NormalizedDocument batches:

~~~text
source
  -> collector registration
  -> sync start
  -> normalized document batch
  -> Core validation
  -> deterministic chunking
  -> embedding/index publication
  -> sync finish
~~~

The contract carries external IDs, title/path/doc type, source type, content and
content hash, modified timestamp, size, deterministic metadata, and optional
Collector/schema/Core protocol versions.

Core enforces batch, document, content, metadata, path, and chunk limits.
Malformed documents are isolated where possible; sync diagnostics record typed
failure reasons instead of silently reporting success.

## Security Boundary

~~~mermaid
sequenceDiagram
  participant U as User
  participant API as Core API
  participant DB as PostgreSQL
  participant C as Collector
  participant M as MCP
  U->>API: JWT-authenticated request
  API->>DB: project membership and RBAC check
  C->>API: scoped Collector token
  API->>DB: source, sync, and document state
  M->>API: scoped Core API request
  API-->>M: authorized Core response
~~~

Collector may read configured source paths and send normalized documents. It
cannot read Core storage directly. MCP calls Core tools only; it cannot ingest,
normalize, access the database, or bypass project RBAC.

Secrets are rejected or redacted before persistence where detected. Audit events
and diagnostics contain metadata and counters, not raw evidence, passwords,
tokens, API keys, or connection strings.

## Storage

PostgreSQL is authoritative for users, memberships, projects, sources,
collectors, syncs, documents, chunk text, retrieval runs/results, workflow
state, evals, audit events, evidence relations, index jobs, and operational
findings/events.

Qdrant stores vector points for retrieval. Vector results are reconciled with
authorized Core chunks before return. Qdrant is not the authorization source.

Redis is used for queue, rate-limit, and runtime paths when configured. Durable
business state remains in PostgreSQL.

## Migration Model

The repository has one clean V2 baseline:

~~~text
alembic/versions/v2/v2_baseline.py
~~~

It creates the complete fresh-install schema. The old 0001-0006 chain was
removed because this repository has no database to preserve. A database built
from the old chain is not compatible with this reset without deliberate
recreation.

~~~bash
alembic upgrade head
python scripts/check_migrations.py
~~~

The ORM models remain in incidentops/db/models.py; Alembic is the versioned
database contract, not a replacement for application models.

## Retrieval Pipeline

~~~mermaid
flowchart LR
  Q[User query] --> I[Intent classifier]
  I --> B[Retrieval budget]
  B --> V[Qdrant vector branch]
  B --> L[PostgreSQL lexical branch]
  B --> X[Exact path and metadata branch]
  B --> G[Deterministic evidence relations]
  V --> F[Score fusion]
  L --> F
  X --> F
  G --> F
  F --> R[Conditional reranking]
  R --> P[Compact evidence pack]
  P --> D[Direct cited response]
  P --> S[Optional Azure OpenAI synthesis]
~~~

Intent classes are code_location, architecture, config_lookup, api_contract,
runtime_incident, deploy_regression, previous_incident, runbook_lookup, and
generic.

Diagnostics include query intent, retrieval budget, source/chunk distributions,
applied boosts/penalties, branch latency, and total retrieval latency. Code
questions favor code/proto/API evidence; runtime questions favor
logs/deploys/incidents/runbooks. Missing runtime evidence produces a warning.

## Models and Inference

Supported model paths:

1. Azure OpenAI for cloud chat and embeddings when configured.
2. Deterministic local-hash embeddings for tests and no-key development.
3. Optional sentence-transformers when explicitly installed/configured.
4. Optional remote reranker endpoint through model_gateway.py.

Removed from this repository:

- AWS Bedrock provider code;
- SageMaker deployment/runtime;
- local model_runtime;
- Azure ML GPU deployment workflow and templates.

No GPU inference service is currently deployed by this repository. Remote
reranking remains an integration hook, not a running endpoint.

## Async Runtime

When queue mode is enabled, the API creates durable job state, enqueues a Redis
job, and returns an identifier. The worker claims the job, executes a bounded
operation, persists status/events/results, and acknowledges or records failure.

Inline execution remains for deterministic tests and development. It is not the
production operating mode.

## Evaluation

The only retained evaluation package is incidentops/eval. It provides the
runner and golden cases used by /v1/evals/run and worker evaluation jobs.
Metrics include evidence recall, term coverage, forbidden hits, source-type hit
rate, zero-result count, citation rate, latency, and model/token counters when
available.

Evaluation detects retrieval regressions; it does not create production
evidence or replace real repository and incident benchmarks.

## Azure IaC and Verified Cloud State

infra/azure/main.bicep describes Azure Container Registry, a Container Apps
environment, Core API, worker, MCP, Collector, migration and bootstrap jobs,
PostgreSQL Flexible Server, Redis, Key Vault, Log Analytics, and a benchmark
job. A legacy frontend Container App definition remains optional but defaults
to disabled; `apps/web` is deployed on Vercel.

The Azure subscription inspected on 2026-08-07 contains:

| Resource group | Resource | Result |
|---|---|---|
| incident-ops | Cognitive Services account incident-ops | present |
| incident-ops | incidentops-chat | gpt-5-mini, succeeded |
| incident-ops | incidentops-embed | text-embedding-3-small, succeeded |
| incidentops-demo-rg | ACR incidentops846e0b9 | present, no repositories listed |

Microsoft.App is NotRegistered. No Container Apps environment, app, or job was
found. No live PostgreSQL, Redis, Qdrant, Core API, worker, Collector, or MCP
runtime was verified. The Vercel console is publicly deployed, but there is no
live application workflow until Core is deployed and its CORS policy allows its
origin.

The Azure OpenAI deployments prove resource provisioning only. They do not prove
an application call, token usage, latency, retrieval quality, or E2E sync.

## CI/CD

.github/workflows/deploy-azure.yml is a manual Azure workflow. It tests Core
and `apps/web`, validates Bicep, builds/pushes the Core image, deploys backend
resources, runs migrations/bootstrap, and runs smoke/E2E checks. It does not
build or deploy a frontend Container App.

It requires Azure OIDC, registry, model, Qdrant, database, and admin
configuration as GitHub secrets/variables. No secret values belong in this
repository. No successful live deployment is claimed here.

## Operational Invariants

1. Project membership is checked before project data is returned.
2. A vector result must map to an authorized Core chunk.
3. Unchanged document hashes are skipped.
4. Changed documents replace prior chunks without duplicates.
5. Purge removes source/project evidence and retains only safe audit metadata.
6. Reindex refreshes Core-held artifacts; full source reparse requires Collector
   reingestion.
7. Production-like startup rejects create_all, local ingest, wildcard CORS,
   memory-only rate limits, inline workers, and local model fallback.
8. Missing logs/deploys/incidents produce warnings rather than invented RCA.

## Current Gaps

Before calling this production-grade, prove a clean Azure deployment, database
and vector/queue connectivity, migration/bootstrap jobs, real Collector sync,
Azure OpenAI embedding and synthesis calls, MCP tools backed by Core, worker
failure/retry recovery, purge/reingestion, backup/restore, and multi-repository
quality/latency metrics.
