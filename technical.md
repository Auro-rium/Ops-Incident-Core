# IncidentOps Technical Architecture

## Status and Scope

IncidentOps is an engineering-evidence system, not a generic chatbot. It
ingests deterministic, redacted evidence from its in-repository Collector runtime, stores and
indexes it in Core, retrieves cited evidence, and provides search,
investigation, workflow, evaluation, readiness, and MCP interfaces.

This document is the single source of technical documentation for the Core
repository. It describes code and intended cloud deployment boundaries. It does
not claim that a cloud component is live unless a successful deployment smoke
has recorded it. As of 2026-07-28, commit `ca0eae2` passed the repository's
Core and frontend GitHub Actions validation workflow. The cloud GPU retrieval
path remains code and deployment scaffolding; no current live Azure proof is
recorded. The known Azure resource groups `incidentops-demo-rg` and
`incidentops-rg` were absent at that check, so there is no deployed Azure
endpoint to infer from this source tree.

### Version context

This repository is the active **v2 architecture rewrite**. The earlier v1
implementation referenced in resume or portfolio material used a
PostgreSQL/pgvector-era vector path and earlier deployment boundaries. This
codebase retires PostgreSQL vector storage in favor of Qdrant, keeps Collector
inside the repository, and adds durable indexing and hybrid retrieval work.
It is a current engineering effort, not a completed production release.

### How to read this document

* **Implemented** means the code and tests exist in this repository.
* **Configured** means settings, manifests, or deployment scripts exist.
* **Validated** requires a recorded runtime test against the target environment.

Unless this document explicitly says a behavior was validated, treat it as
implemented or configured only. This distinction is intentional: deployment
scaffolding and a passing local harness do not prove cloud reliability.

## Repositories and Responsibilities

| Repository | Owns | Does not own |
|---|---|---|
| `Ops-Incident-Core: incidentops.collector` | source access, path policy, redaction, deterministic metadata extraction, `NormalizedDocument` production, sync checkpoints | retrieval, embeddings, model calls, incident diagnosis, database access |
| `Ops-Incident-Core: Core services` | auth/RBAC, projects, sources, syncs, parsing/chunking, indexing, retrieval, investigation, workflows, evals, audit, MCP facade | arbitrary filesystem crawling outside configured Collector roots |
| `Ops-Incident-Core: apps/web` | minimal operator-facing UI over Core APIs | evidence normalization or authorization bypass |

The Collector-to-Core boundary is intentional even though both runtimes share
one repository and image: Collector has access to source material; Core has
project-scoped data and retrieval authority. Collector uses authenticated HTTP
only and cannot access PostgreSQL directly. MCP is a Core interface only. It
must call Core APIs with scoped credentials and cannot
ingest, normalize, access PostgreSQL directly, or bypass RBAC.

## End-to-End Flow

```mermaid
flowchart LR
  subgraph Source Environment
    R[Repository, logs, docs, config, deploy data]
    C[Collector]
  end
  subgraph Core
    API[FastAPI Core API]
    W[Core Worker]
    PG[(PostgreSQL metadata and lexical search)]
    QD[(Qdrant vector store)]
    RS[(Redis Streams / rate limits)]
    MCP[Core MCP]
  end
  subgraph Model Plane
    AOAI[Azure OpenAI chat]
    AML[Azure ML GPU embedder/reranker]
  end
  UI[Frontend] --> API
  R --> C
  C -->|redacted NormalizedDocument batch| API
  API --> PG
  API --> QD
  API --> RS
  RS --> W
  W --> PG
  W --> AML
  API --> AML
  API --> AOAI
  MCP -->|Core API only| API
```

### Ingestion lifecycle

1. A user creates a project and source, then a Collector is registered.
2. Collector discovers allowed files, redacts secrets, extracts deterministic
   metadata, and sends `NormalizedDocument` batches with source/sync metadata.
3. Core validates document size, metadata, path, batch limits, and duplicate
   external IDs. It records typed failures rather than silently dropping them.
4. Core parses, chunks, embeds, and publishes vectors through a durable index
   job when asynchronous indexing is enabled; the API does not wait for remote
   GPU embedding in that mode.
5. The worker dispatches durable job IDs through Redis Streams, recovers
   pending/abandoned jobs from Postgres, obtains embeddings, replaces changed
   document chunks transactionally, and updates sync diagnostics.
6. A finished sync reports source coverage, parser/error counts, chunk counts,
   unchanged skips, and async indexing state.

Raw documents are not sent in Redis job payloads. The durable database job is
the source of truth; Redis is transport and recovery coordination.

### Search and answer lifecycle

```mermaid
sequenceDiagram
  participant U as User or MCP Client
  participant API as Core API
  participant Q as Intent Router
  participant V as Qdrant Vector Search
  participant L as PostgreSQL Full Text Search
  participant X as Metadata and Graph Search
  participant RR as GPU Reranker
  participant M as Azure OpenAI

  U->>API: query and project ID
  API->>Q: classify query
  Q->>V: vector budget
  Q->>L: lexical budget
  Q->>X: exact metadata/path; graph for architecture only
  V-->>API: vector candidates
  L-->>API: lexical candidates
  X-->>API: deterministic candidates
  API->>API: weighted RRF and source-aware boosts
  opt ambiguous candidate set
    API->>RR: bounded rerank request
    RR-->>API: scores
  end
  API->>API: compact cited evidence pack
  alt exact code/config/API lookup
    API-->>U: direct cited evidence answer
  else synthesis justified
    API->>M: compact evidence-only prompt
    M-->>API: constrained answer
    API-->>U: cited response with warnings
  end
```

## Data Contract

`NormalizedDocument` is the universal ingestion contract. It carries an
external ID, path, source type, content hash, normalized/redacted content,
metadata, source size, and optional modification time. Collector protocol
metadata (`collector_version`, `schema_version`, `core_api_version`) is stored
in sync diagnostics but is not hard-rejected for older Collectors.

Core data ownership is project-scoped:

| Data | Purpose |
|---|---|
| `projects`, memberships, users | project isolation and RBAC |
| `sources`, collectors, `source_syncs` | source registry and ingestion lifecycle |
| `documents`, `chunks` | authoritative evidence, chunk metadata, and lexical index |
| Qdrant collection | embeddings and bounded vector filter payloads |
| `index_jobs` | durable async parse/embed/publish work |
| runs, events, approvals, evals | workflow and evaluation state |
| `operational_runs`, `operational_findings`, `operational_events` | durable evaluator/observer/logging state and redacted runtime observations |
| audit events | security and destructive-operation traceability |

Changed `external_id` content replaces old chunks before publishing new chunks.
Unchanged content hashes are skipped. Source/project purge deletes retrieval
state and evidence while retaining a safe audit tombstone without raw content.
The current reindex endpoint refreshes existing chunks and search vectors; it
does not claim to refetch raw source data. Full reparse requires Collector
reingestion.

## Parsing and Chunking

Parsing is deterministic. No LLM normalizes source files.

| Source class | Main chunk types | Important metadata |
|---|---|---|
| Go | `go_module`, `go_type`, `go_function`, `go_method` | package, symbol, line range |
| Python/TS/Java | module/class/function chunks plus fallback | language, symbol, line range |
| Proto/OpenAPI | `proto_service`, `proto_rpc`, message/enum, API endpoint | package, endpoint, line range |
| Markdown | heading sections | title, heading, line range |
| Logs | `log_window`, bounded `error_cluster` | timestamps, levels, trace IDs, deploy hash, endpoint |
| JSON/YAML/TOML/INI | configuration or endpoint sections | config key, service, endpoint |
| Deploys/diffs | `deploy_diff` | deploy hash, service, changed files |
| Incidents | `incident_section` | symptom, root cause, timeline/action section |

Generated, binary, empty, oversized, malformed, or unsupported files are
reported using typed diagnostics. Important failure categories are
`unsupported_extension`, `generated_file`, `oversized`, `empty`, `binary`,
`malformed_content`, `parser_exception`, `metadata_invalid`,
`chunk_limit_exceeded`, `embedding_failed`, and `unknown`.

Fallback chunks are preferred to silent data loss, but chunk count and token
limits prevent a single malformed file from creating an unbounded index.

## Retrieval Design

### Vector storage

PostgreSQL is authoritative for chunk text, metadata, authorization, and
citations. Qdrant stores embedding vectors and bounded retrieval payloads.
Every vector hit is mapped back to an authorized PostgreSQL chunk before it is
returned. PostgreSQL does not store vector embeddings;
runtime indexing and retrieval use Qdrant.

### Query intent and budgets

The router classifies requests as `code_location`, `architecture`,
`config_lookup`, `api_contract`, `runtime_incident`, `deploy_regression`,
`previous_incident`, `runbook_lookup`, or `generic`.

| Intent | Preferred evidence |
|---|---|
| code location | code, proto/API chunks, exact symbols and paths |
| architecture | docs, packages, services, code boundaries |
| config lookup | config, deploy manifests, code/docs |
| API contract | OpenAPI/proto/API code |
| runtime incident | logs, deploys, incidents, runbooks, then code |
| deploy regression | deploy history/diffs, logs, code |
| previous incident | incident reports, runbooks, logs |

Vector, lexical, and exact metadata/path searches execute in parallel when
independent database sessions are available. Architecture queries also run a
bounded deterministic graph branch. Weighted reciprocal-rank fusion uses
vector `0.40`, lexical `0.30`, metadata/path `0.20`, and graph `0.10` when the
graph branch applies. Source type, chunk type, metadata, exact path/symbol,
service, endpoint, and deploy matches add bounded boosts. Generic README or
boilerplate evidence is penalized for code or runtime questions unless it is
an exact match.

The graph stores only direct facts derived during indexing: `contains`,
`defines`, `implements`, and `changed_by` edges across chunk IDs, paths,
services, packages, symbols, endpoints, and deploy hashes. It does not infer
call graphs, dependencies, or incident causality.

The reranker is conditional: it runs only for ambiguous candidate sets. Exact
or decisive code/config/API evidence uses a direct evidence path. Evidence is
deduplicated and constrained to 5-8 chunks, 1200-1800 characters per chunk,
and 8k-12k total characters before model synthesis.

### Diagnostics and metrics

Search debug output contains query intent, source/chunk distributions,
retrieval budget, boosts/penalties, branch timings, and total retrieval
latency. It excludes raw secrets and does not expose credentials.

Metrics cover ingest counts/failures, retrieval intent/source/chunk mix,
embedding/rerank calls and failures, cache hits/misses, workflow/eval events,
and average observed latencies. Query-class evaluations report evidence recall,
term coverage, source-type hit rate, wrong-source-type rate, p50/p95 latency,
and forbidden-term hits.

## Model Boundary

Core and Collector must not load production embedding or reranking models.
The intended model plane is Azure ML managed GPU endpoints:

| Endpoint | Model role | Contract |
|---|---|---|
| embedding endpoint | BGE-M3 compatible encoder | bounded text list -> 1024-dimension vectors |
| reranker endpoint | BGE reranker compatible cross encoder | query plus bounded candidates -> one score per candidate |
| Azure OpenAI | answer synthesis | compact evidence-only answer prompt -> cited response |

The endpoint runtime requires immutable Hugging Face revisions. Local model
loading is disabled in production and rejected by startup validation. A
deterministic local-hash path remains for development/CI compatibility only.

GPU retrieval requirements:

```text
RETRIEVAL_BACKEND=qdrant
QDRANT_URL=https://your-qdrant-endpoint
QDRANT_COLLECTION=incidentops_chunks
VECTOR_INDEX_VERSION=current
EMBEDDING_MODEL=azure-ml-bge-m3
RERANKER_MODEL=azure-ml-bge-reranker-v2-m3
RAG_GPU_ENDPOINT_REQUIRED=true
RAG_ASYNC_INDEXING=true
RAG_MODEL_REVISION=<immutable revision>
EMBEDDING_DIM=1024
```

The Azure ML manifests set private endpoint access. Container Apps therefore
need VNet integration, private DNS, and private connectivity to Azure ML. The
current budget-demo IaC does not yet provision that path. GPU RAG must remain
disabled until it does.

## API and Security

Core uses JWT authentication and project-scoped RBAC. Viewers can read allowed
project data; elevated roles are required for ingestion operations; project
admins are required for purge/reindex actions. The service enforces request,
query, batch, document, metadata, and diagnostics limits.

Security controls include bcrypt password hashing, source configuration secret
rejection, Collector redaction, output sanitization, prompt-injection flags,
rate limiting, audit events, and no raw evidence in audit metadata. Local
folder ingestion is not permitted in production. Metrics are private by
default. MCP has a separate scoped token and calls Core HTTP APIs only.

Important endpoints include:

```text
GET    /health
GET    /ready
GET    /v1/capabilities
GET    /v1/runtime/status
GET    /v1/projects
GET    /v1/projects/{project_id}/readiness
POST   /v1/projects/{project_id}/sources
POST   /v1/projects/{project_id}/collectors/register
POST   /v1/sources/{source_id}/syncs/start
POST   /v1/sources/{source_id}/documents/batch
POST   /v1/sources/{source_id}/syncs/{sync_id}/finish
DELETE /v1/projects/{project_id}/sources/{source_id}
DELETE /v1/projects/{project_id}
POST   /v1/projects/{project_id}/sources/{source_id}/reindex
GET    /v1/projects/{project_id}/sources/{source_id}/integrity
POST   /v1/search
POST   /v1/answer
POST   /v1/investigate
POST   /v1/runs
GET    /v1/projects/{project_id}/operations/runs
GET    /v1/projects/{project_id}/operations/findings
POST   /v1/projects/{project_id}/operations/observer/runs
POST   /v1/projects/{project_id}/operations/logging/runs
```

`/v1/runtime/status` is authenticated and returns only safe configuration
state, such as selected provider/backend, worker mode, and local-fallback
status. It never returns tokens, connection strings, passwords, or API keys.

## Operational Agents and Runtime Hardening

Phase 4 adds three bounded operational agents. They are worker jobs with
durable project-scoped records, not autonomous services with data-plane
authority.

| Agent | Input | Output | Explicit non-authority |
|---|---|---|---|
| Evaluator | persisted query-class evaluation cases | recall, source-type, zero-result, citation, latency, and deterministic model-usage summaries | cannot change ranking or configuration |
| Observer | recent syncs, index-job state, and latest evaluation summary | typed threshold findings and recommended actions | cannot delete, reindex, or modify evidence |
| Logging aggregate | bounded, redacted operational events | category/severity/event-type counts | cannot retain raw documents or prompts |

`operational_events` records bounded metadata from ingestion, async indexing,
search, answer, investigation, evaluator, observer, and worker failures. The
redaction boundary removes content, evidence, prompts, credentials, tokens,
passwords, API keys, authorization values, and connection strings before an
event is persisted. Events are useful for identifying where a pipeline is
degrading; they are not an evidence archive.

Worker jobs use per-type timeouts. Indexing, evaluator, observer, and logging
jobs can retry within `WORKER_JOB_MAX_RETRIES`; workflow approval actions are
not retried by this mechanism. Terminal job failures store only a safe error
code and emit a redacted operational event. The API exposes read-only
operational runs and findings to project viewers and restricts job submission
to project administrators.

OpenTelemetry spans cover worker dispatch, query embeddings, retrieval score
fusion, remote reranking, and evaluator execution when `ENABLE_OTEL=true`.
Metrics are best effort: a metrics failure must never fail a request or a
worker job. Project cache invalidation occurs after indexing changes, source
purge, project purge, and reindex refresh. At this stage query classification
is cached project-safely; full retrieval-result caching remains a later,
measured optimization rather than an undocumented correctness risk.

## Runtime and Deployment

The intended Azure stack is Azure Container Apps for Core API, worker,
Collector, frontend, MCP, migration/bootstrap jobs; PostgreSQL Flexible Server;
Azure Cache for Redis; Key Vault; ACR; Log Analytics; Azure OpenAI; and
optional Azure ML GPU endpoints. Qdrant is a required, externally supplied
private HTTPS endpoint (`QDRANT_URL` and optional API key); the current Bicep
template configures consumers for it but does **not** provision a Qdrant
service. AKS, NAT Gateway, multi-region deployment, and Azure AI Search are
deliberately excluded from the current architecture.

The checked-in Bicep is a **deployment scaffold, not a private production
network design**. It currently enables public-network access for PostgreSQL,
Redis, and Key Vault, while Core API and frontend use external Container Apps
ingress. PostgreSQL permits Azure services through a firewall rule; Redis and
Key Vault still rely on credentials/RBAC rather than private networking. This
does not satisfy the intended internal-only data-plane requirement. Do not
deploy it as production until VNet integration, private endpoints, restrictive
firewall rules, private DNS, and a security review are implemented and tested.

Deployment order:

1. Build the Core image, which contains API, worker, and Collector commands.
   The optional frontend and GPU runtime images are separate builds.
2. Provision or select a private Qdrant endpoint, then provide its URL and
   credential reference to the deployment. Do not make it public merely to
   satisfy Container Apps connectivity.
3. Deploy infrastructure and Container Apps using Key Vault references.
4. Run `alembic upgrade head` and `scripts/check_migrations.py` in the
   migration job. Production never calls SQLAlchemy `create_all`.
5. Bootstrap an admin using Key Vault-backed credentials.
6. Configure scoped Collector and MCP credentials.
7. Run health, readiness, Collector sync, search, investigate, workflow, and
   MCP smoke checks.

GitHub Actions runs lint, migrations, API startup, unit/integration tests, and
a Next.js production type/build check on pushes to `core`. Run `30384027371`
passed those CI jobs on 2026-07-28. Azure build/deploy/migration/smoke is
manual-only through `workflow_dispatch`; this prevents an ordinary code push
from creating cloud resources or costs. The manual deployment job builds the
Core and frontend images, validates both Bicep templates, runs
migrations/bootstrap/smoke, and can run the bounded release benchmark only when
`run_release_benchmark=true`. It will create billable resources when the
resource group is absent. A successful manual run with retained measurements is
required before claiming live Azure validation.

### Browser Boundary

The Next.js console in `apps/web` is a same-origin client of Core. Its
`/api/[...path]` route forwards selected browser headers to the fixed runtime
`CORE_API_BASE_URL`; it does not proxy arbitrary URLs. Qdrant, PostgreSQL,
Redis, Collector, and model credentials are never browser configuration. The
container serves port 3000, so Azure Container Apps ingress targets port 3000.
The console displays Core-backed project/readiness/source/search/investigation/
workflow/evaluation/operations state and intentionally has no local-path ingest
control.

### Benchmark Harness Boundary

`python -m incidentops.collector benchmark` is a release harness, not Collector
daemon behavior. It can query Core only after a sync to record authorized search
and source-integrity counters. The production Collector daemon still only
discovers, redacts, normalizes, batches, and syncs evidence. Integrity output is
aggregate-only: document/chunk counts and duplicate chunk rows grouped by
document, type, line range, and text digest; it never returns source text.

## Evaluation and Proof Standard

Publishing a benchmark requires real values for:

| Category | Required metrics |
|---|---|
| Ingestion | files seen/skipped, skip reasons, normalized documents, chunks, parser/embedding failures, redactions, sync duration |
| Idempotency | unchanged skips, changed-file replacement, duplicate chunks |
| Retrieval | recall@5, source-type hit rate, wrong-source-type rate, zero-result rate, p50/p95 latency |
| Answer | citation count, provider/model, prompt/completion tokens when supplied, answer latency |
| Operations | readiness score, worker/index failures, cache hit rate, MCP tool success |

Two public repository benchmarks are useful for code/docs ingestion. They do
not prove runtime RCA. A genuine incident evidence pack with logs, deploy
history, runbooks, and postmortems is required before claiming root-cause
capability. This repository contains no retained, reproducible
multi-repository Azure benchmark report, so no Temporal-scale metric is current
product proof.

## Current Limits and Next Work

The system is not production-grade yet. Blocking gaps are:

1. Deliberately reprovision Azure, configure Azure OIDC and explicit deployment
   variables, then deploy the stack successfully through the manual workflow.
2. Add VNet/private endpoint networking before enabling Azure ML GPU
   endpoints.
3. Run clean multi-repository ingestion and query-class evaluations with
   published measurements.
4. Validate async index recovery, cache invalidation, purge, and reingestion
   under failure/restart conditions in Azure.
5. Add evidence packs with real logs/deploys/incidents before claiming runtime
   RCA quality.
6. Validate Qdrant vector publication and retrieval consistency before comparing
   it to additional managed retrieval services.

Until those are complete, the correct product claim is: IncidentOps is a
security-conscious, Collector-first engineering evidence backend with an
implemented but not live-validated cloud GPU retrieval path.

## Implementation Ledger

| Area | Present in code | Validated in current repository | Important boundary |
|---|---|---|---|
| Collector, redaction, and normalized batch protocol | Yes | Unit/integration coverage | Collector cannot access Core storage directly. |
| PostgreSQL metadata, chunks, full-text search, and audit | Yes | Unit/integration coverage | PostgreSQL is authoritative for text, authorization, and citations. |
| Qdrant vector adapter and Qdrant migration | Yes | Local/test coverage | The current Bicep template does not create Qdrant. |
| Hybrid retrieval and direct evidence graph | Yes | Unit/integration coverage | The graph contains direct deterministic facts only. |
| Redis Streams queue and durable index jobs | Yes | Unit/integration coverage | Postgres remains the business-state authority. |
| Azure OpenAI and Azure ML client contracts | Yes | Configuration/startup validation | No current live Azure proof is recorded. |
| Azure Container Apps infrastructure and scripts | Yes | Bicep/script validation; no deployed resource group | Current Bicep networking is not production-private. |
| Phase 4 operational agents and event redaction | Yes | Unit/integration coverage | Cloud durability and thresholds still need live Azure validation. |
| MCP facade | Yes | Unit/integration coverage | MCP delegates to Core HTTP APIs and cannot ingest. |
| Operator console and same-origin proxy | Yes | Frontend build plus isolated local proxy smoke | No live Azure frontend is currently deployed. |

## Operational Invariants

The following rules are architectural constraints, not optional conventions:

1. A vector hit is not returned until it maps to an authorized PostgreSQL chunk.
2. Collector redaction and path policy precede Core ingestion.
3. A changed document replaces prior chunks for the same external ID; unchanged
   hashes are skipped.
4. Redis transports work, but Postgres records the durable index-job state.
5. Purge removes project/source evidence from retrieval stores and retains only
   an audit tombstone without raw evidence.
6. Reindex refreshes existing Core-held artifacts. It is not a claim that Core
   can re-read an external source; full reparse requires Collector reingestion.
7. Production configuration rejects local model loading, memory-only rate
   limits, inline workers, wildcard CORS, local ingest, and `create_all`.
8. When logs, deploy data, or incident history are absent, investigation must
   return a missing-evidence warning rather than unsupported root-cause certainty.
