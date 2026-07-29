# IncidentOps Core

**Engineering evidence for incident investigation, without treating a language model as a source of truth.**

IncidentOps ingests code, configuration, API contracts, runbooks, logs, deploy records, and incident notes through a deterministic Collector. It returns project-scoped evidence with citations, explicit uncertainty, and retrieval diagnostics.

It is built for the moment after an engineer asks: *What changed? Where is the relevant code? What evidence do we actually have?* The answer should be grounded in files and metadata, not a confident summary with no trail back to reality.

> **Version context:** This repository is the active **v2 architecture rewrite**. If you encountered IncidentOps v1 in a resume or portfolio, that refers to the earlier PostgreSQL/pgvector-era implementation and deployment work. This codebase replaces vector storage with Qdrant, brings the Collector into this repository, and is still under active hardening. It must not be read as a completed production release.

> **Current status (2026-07-29):** Phase 6 AWS implementation is under validation. The repository now contains AWS provider contracts for Bedrock and SageMaker, Terraform for the private data plane and ECS runtimes, and a manual OIDC deployment workflow. No live AWS deployment or performance result is claimed yet. The prior Azure files remain only until the AWS release gates pass. It is **not yet production-grade**. See [What we do not claim](#what-we-do-not-claim).

## Why IncidentOps

Most incident tooling has operational data with little code context, or code search with no understanding of the evidence needed to explain a runtime failure. IncidentOps keeps those concerns explicit.

It helps teams:

- establish what evidence a project contains and what is missing;
- find the code, configuration, API contract, or runbook relevant to a question;
- distinguish a code-navigation question from a runtime incident question;
- return cited evidence and say when logs, deploy history, or prior incidents are absent;
- measure retrieval quality with deterministic evaluations;
- use an MCP facade over Core without turning MCP into ingestion or an authorization bypass.

The governing principle is: **deterministic ingestion first; model use only after evidence is selected and bounded.**

## At a Glance

| Area | Responsibility |
|---|---|
| In-repository Collector | Enforces path policy, redacts secrets, extracts deterministic metadata, and sends `NormalizedDocument` batches over authenticated HTTP. |
| Core API | Owns auth/RBAC, projects, sources, sync state, chunking, indexing, search, investigation, workflows, evals, readiness, audit, and MCP-facing APIs. |
| Retrieval | Combines Qdrant vector candidates, PostgreSQL lexical search, exact metadata/path matches, and a bounded architecture graph with weighted reciprocal-rank fusion. |
| Answers | Uses direct cited evidence for decisive code/config/API lookups; optional synthesis is evidence-only and compact. |
| Operations | Provides typed ingestion failures, sync diagnostics, readiness reports, workers, queues, evaluator/observer/logging runs, metrics, traces, purge, and reindex semantics. |
| Operator console | Uses same-origin `/api` proxying to Core and surfaces readiness, retrieval, investigations, evaluations, and operational findings without browser access to storage or model credentials. |

## Architecture

```mermaid
flowchart LR
  User[Engineer or MCP client] --> UI[Minimal web UI]
  UI --> API[FastAPI Core API]
  Source[Repo, docs, logs, config, deploys] --> Collector[Collector]
  Collector -->|redacted NormalizedDocument batches| API
  API --> PG[(PostgreSQL: project data, chunks, FTS, audit)]
  API --> QD[(Qdrant: vectors)]
  API --> Redis[(Redis: queue, cache, rate limits)]
  Redis --> Worker[Core worker]
  Worker --> PG
  Worker --> QD
  API --> Router[Intent router and retrieval budgets]
  Router --> Fusion[Vector + lexical + metadata + graph: weighted RRF]
  Fusion --> Evidence[Compact cited evidence pack]
  Evidence --> Answer[Direct evidence answer or optional Bedrock synthesis]
  MCP[Core MCP facade] -->|scoped Core API calls only| API
```

The Collector and Core deliberately have different authority boundaries. Collector can read an approved source root but cannot query the database or diagnose incidents. Core owns project isolation and retrieval. MCP only calls Core APIs with scoped credentials: it cannot ingest, normalize, access storage directly, or bypass RBAC.

## Evidence Lifecycle

1. A user creates a project and source, then registers a Collector.
2. The Collector discovers allowed files, redacts secrets, extracts deterministic metadata, and sends normalized documents.
3. Core validates limits and metadata, records typed errors, and skips unchanged content by hash.
4. Deterministic parsers create source-aware chunks: code functions/types, Protobuf services/RPCs, Markdown sections, log windows/error clusters, and structured configuration blocks.
5. PostgreSQL stores authoritative text and access-controlled metadata. Qdrant stores vectors. Workers can perform durable asynchronous indexing.
6. Core classifies the query before retrieval. Code questions favor code and API chunks; runtime questions favor logs, deploys, incidents, and runbooks.
7. The response contains citations and safe diagnostics. A model is used only when synthesis adds value and the evidence pack is bounded.

## Retrieval, Intentionally

IncidentOps is not an “embed everything and pray” system. It uses bounded retrieval branches, then fuses their candidate rankings:

- **Vector retrieval:** semantic candidates from Qdrant.
- **Lexical retrieval:** PostgreSQL full-text candidates for exact terms and code-like queries.
- **Metadata/path retrieval:** exact symbols, packages, services, endpoints, and path segments.
- **Architecture graph:** direct deterministic `contains`, `defines`, `implements`, and `changed_by` relations. It does not invent a call graph or incident causality graph.

The intent router supports `code_location`, `architecture`, `config_lookup`, `api_contract`, `runtime_incident`, `deploy_regression`, `previous_incident`, `runbook_lookup`, and `generic`. A broad README should not win a code-location query merely because it is broad and well-written.

Reranking is conditional. Exact symbol, path, configuration, and API-contract matches take the fast direct-evidence path instead of paying model latency for every request.

## What You Can Build With It Today

- Collector-driven ingestion of repository, document, config, and incident evidence using the `NormalizedDocument` contract.
- Project-scoped search, answer, and investigation APIs with citations.
- Readiness reports that expose coverage gaps, weak question classes, and next evidence to ingest.
- Workflow runs with durable events and approval gates for risky actions.
- Query-class evaluation fixtures for retrieval measurement.
- Project/source purge and reindex endpoints with admin authorization.
- A small, safe MCP server that proxies Core capabilities.
- Deterministic local-hash embeddings for development and CI without a paid key.

## Operator Console

`apps/web` is a compact Next.js operations console, not a separate source of
truth. It stores a login token only in browser session storage and proxies every
request through its same-origin `/api/*` route to `CORE_API_BASE_URL`. The
browser does not receive Qdrant, PostgreSQL, Redis, Collector, Bedrock, or
GPU endpoint credentials.

After sign-in, the console can create or select a membership-scoped project and
display Core-backed runtime status, readiness coverage/gaps, Collector sources,
cited search results, cautious investigations, workflow events, evaluations,
and observer findings. It intentionally has no local-folder ingestion control:
production ingestion remains an authenticated Collector workflow.

## What We Do Not Claim

- A repository alone cannot explain a runtime outage. Honest root-cause work needs logs, deploy/change context, and often previous incident material.
- This service has not earned a production-grade claim until the AWS deployment, async index recovery, purge/reingestion, cache invalidation, backup/restore, and multi-repository evaluations are measured under failure conditions.
- The Bedrock and private SageMaker model paths are code and deployment configuration, **not current live proof**.
- MCP is not an ingestion path, an agentic database back door, or an RBAC bypass.
- The current web UI is an operator surface, not a finished product UI.

## Quick Start: Local Development

The Compose stack is for development only. It runs PostgreSQL, Qdrant, Redis, the API, a worker, and a minimal web UI. Do not expose this development setup to the public internet or use its credentials in a real environment.

### Prerequisites

- Docker Engine with the Compose plugin
- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) for local checks

### Start the stack

```bash
git clone <your-fork-or-remote> inspection-ops
cd inspection-ops
cp .env.example .env
docker compose up -d --build postgres qdrant redis
docker compose run --rm --no-deps api alembic upgrade head
docker compose up -d --build api core-worker web
```

Create a development-only administrator using a password you choose:

```bash
docker compose exec -e BOOTSTRAP_ADMIN_EMAIL=admin@example.test \
  -e BOOTSTRAP_ADMIN_PASSWORD='choose-a-local-password' \
  api python -m incidentops.security.bootstrap_admin
```

Confirm the API and dependencies are ready:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
curl http://127.0.0.1:8000/v1/capabilities
```

The web UI is at `http://127.0.0.1:3000`; the OpenAPI document is at `http://127.0.0.1:8000/openapi.json`.

For an API-only smoke flow after starting the stack:

```bash
python scripts/smoke_prod.py --base-url http://127.0.0.1:8000 \
  --email admin@example.test --password 'choose-a-local-password' \
  --query 'What does this tiny service evidence say?'
```

The smoke creates an isolated project and sends a tiny normalized batch; it does not require local-folder ingestion.

### Run checks

The integration tests run from the host and open direct database sessions, so
override the Compose-container hostname before running them:

```bash
export DATABASE_URL="postgresql+asyncpg://incidentops:incidentops@127.0.0.1:${POSTGRES_PORT:-5433}/incidentops"
uv run --extra dev ruff check .
uv run --extra dev python -m pytest tests/unit tests/integration -q
uv run python -m compileall incidentops apps scripts
docker compose exec api python scripts/check_migrations.py
```

The checked-in `.env.example` is Compose-oriented: its database hostname is
`postgres`, the Compose service name. For a host-run command, set
`DATABASE_URL` explicitly to `127.0.0.1:${POSTGRES_PORT:-5433}` or run the
command inside the API container as shown above. Never use this local file as a
production environment file.

## Core API Tour

Project data routes require a bearer token and project membership. The complete typed API is in OpenAPI; these paths are the usual integration flow:

| Task | Endpoint |
|---|---|
| Login | `POST /v1/auth/login` |
| Create a project | `POST /v1/projects` |
| List accessible projects | `GET /v1/projects` |
| Register a source | `POST /v1/projects/{project_id}/sources` |
| Register a Collector | `POST /v1/projects/{project_id}/collectors/register` |
| Start, upload, finish a sync | `POST /v1/sources/{source_id}/syncs/start`, `POST /documents/batch`, `POST /finish` |
| Inspect readiness | `GET /v1/projects/{project_id}/readiness` |
| Search evidence | `POST /v1/search` |
| Produce a cited answer | `POST /v1/answer` |
| Run a cautious investigation | `POST /v1/investigate` |
| Inspect safe runtime status | `GET /v1/runtime/status` |
| Purge evidence | `DELETE /v1/projects/{project_id}/sources/{source_id}`, `DELETE /v1/projects/{project_id}` |
| Inspect source integrity | `GET /v1/projects/{project_id}/sources/{source_id}/integrity` |
| Run operational checks | `POST /v1/projects/{project_id}/operations/observer/runs`, `POST /v1/projects/{project_id}/operations/logging/runs` |
| Inspect operational results | `GET /v1/projects/{project_id}/operations/runs`, `GET /v1/projects/{project_id}/operations/findings` |

`/v1/runtime/status` reports safe configuration state only. It never returns keys, connection strings, passwords, tokens, or raw document content.

## Security and Data Handling

- JWT authentication, bcrypt password hashing, project-scoped RBAC, and audit events protect the Core surface.
- Collector path policy and secret redaction run before content reaches Core.
- Source configuration validation rejects likely embedded credentials.
- Diagnostics are size-limited and omit raw secrets and large evidence bodies.
- Local folder ingestion is disabled in production-like environments; metrics are private by default.
- Purge removes evidence and retrieval state while retaining only a safe audit tombstone, never raw deleted content.

See [technical.md](technical.md#api-and-security) for the full boundary model.

## Model, Cloud, and CI Boundary

Development and tests use deterministic local-hash embeddings. The Phase 6
production target is AWS:

- Amazon Bedrock Titan Text Embeddings V2 at 1024 dimensions.
- Amazon Bedrock Claude-compatible synthesis through the Converse API.
- A private SageMaker GPU endpoint for conditional BGE reranking.
- ECS Fargate for API, worker, Collector, frontend, and MCP; RDS PostgreSQL,
  TLS ElastiCache Redis, and private self-hosted Qdrant on encrypted EBS.

Core and Collector do not load production models. ECS task roles call Bedrock
and SageMaker through IAM; no model API key is injected. Qdrant, PostgreSQL,
Redis, Collector, worker, MCP, and SageMaker are private. The public ALB reaches
only the frontend; the frontend's same-origin `/api` route calls Core through
private service discovery.

Pushes to `core` run the `Validate and Manually Deploy AWS` validation jobs:
Core checks, frontend build, and Terraform validation. AWS apply is
`workflow_dispatch` only, protected by the `aws-production` environment, and
uses GitHub OIDC. It builds immutable ECR images, applies Terraform, runs
migrations/readiness, bootstraps the administrator, proves Bedrock and optional
SageMaker model contracts, promotes ECS services, and runs smoke. This workflow
exists but has not yet produced a retained successful live deployment record.
Azure deployment workflows are manual-only and are retained temporarily as a
rollback reference; they are removed only after every AWS retirement gate in
[plan.md](plan.md#azure-retirement-gate) passes.

The one-time CI bootstrap is intentionally separate from the main Terraform
state. An AWS administrator runs `make aws-bootstrap-cicd` once. It creates an
encrypted, versioned, public-blocked S3 state bucket and a GitHub OIDC role whose
trust policy accepts only the `Auro-rium/Ops-Incident-Core` repository's
`aws-production` environment. The helper records only non-secret GitHub
variables; it never uploads AWS access keys. Configure exact `CORS_ORIGINS` and
an optional budget email as repository variables before dispatching the apply.

## Documentation Map

- [technical.md](technical.md): authoritative architecture, data contracts, security boundaries, retrieval design, deployment state, and known limits.
- [plan.md](plan.md): six-phase delivery plan and current validation state, including the unresolved Azure proof and later AWS-only release gates.
- [`.env.example`](.env.example): safe local-development settings shape.
- [`.env.aws.production.example`](.env.aws.production.example): AWS production contract without secrets.
- [`.env.production.example`](.env.production.example): retained Azure contract until the AWS cutover gate passes.
- [`eval/query_classes/`](eval/query_classes): deterministic retrieval fixtures.

## Contributing

Three rules keep this system useful:

1. **Evidence stays attributable.** Results map back to authorized project evidence with useful citations.
2. **Ingestion stays deterministic.** Parsing, normalization, and secret handling do not move into an LLM prompt.
3. **Claims stay measurable.** Ranking, model, and deployment changes need tests and a stated validation boundary; never turn a local harness result into a cloud performance claim.

Keep generated caches, environment files, benchmark worktrees, and credentials out of Git. This repository does not currently declare a license; confirm usage and distribution terms with the owner before reusing it.

The two Markdown files under `tests/fixtures/basic_incident/` are synthetic
test evidence, not operational runbooks or incident history for a real system.
