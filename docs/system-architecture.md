# IncidentOps Core System Architecture

IncidentOps Core is the backend brain of the IncidentOps system. It is designed as a collector-first RAG backend for incident investigation, not as a generic chatbot glued to a vector database because apparently society has suffered enough of those.

## System boundary

IncidentOps is split into three repositories and runtime concerns:

```text
Ops-Incident-Collector
  reads company data near the source
  filters unsafe files
  redacts secrets
  normalizes documents
  syncs batches to Core

Ops-Incident-Core
  stores source and sync state
  validates normalized documents
  chunks and indexes evidence
  retrieves evidence
  investigates incidents
  persists workflow/eval/audit state

Ops-Incident-frontend
  operator console
  calls Core APIs
  shows health, syncs, evidence, investigations, and workflow runs
```

Core must not become a crawler. Production data access belongs in Collector. Core receives normalized batches and owns indexing, retrieval, investigation, workflow, security, evals, and observability.

## High-level flow

```text
engineering evidence
  -> Collector
  -> NormalizedDocument batch
  -> Core validation
  -> idempotent indexing
  -> documents + chunks
  -> pgvector + full-text retrieval
  -> evidence packing + citations
  -> investigation response
  -> workflow run events / approvals
```

## Main services

```text
api
  FastAPI app exposing auth, projects, sources, syncs, search, investigate,
  workflow runs, evals, metrics, health, readiness, and capabilities.

core-worker
  Background worker for workflow and eval jobs when queue mode is enabled.

postgres
  PostgreSQL with pgvector extension. Stores users, projects, sources,
  collectors, source syncs, documents, chunks, runs, approvals, evals,
  audit events, and retrieval state.

redis
  Queue/rate-limit/runtime backing service in production-style mode.

collector
  Separate runtime from the Collector repo. Sends normalized evidence to Core.

frontend
  Separate operator console from the frontend repo in the EC2 demo path.
```

## Data model overview

Important tables:

- `users`: authenticated users
- `projects`: tenant/project isolation boundary
- `project_members`: RBAC membership
- `sources`: logical engineering evidence sources
- `collectors`: registered Collector runtimes
- `source_syncs`: sync attempt state, diagnostics, coverage, counters
- `documents`: normalized document records keyed by source/external identity
- `chunks`: searchable evidence units with metadata, text, vectors, and full-text fields
- `agent_runs`: persisted workflow/investigation run state
- `agent_run_events`: node/run event timeline
- `approvals`: approval gates for risky workflow actions
- `eval_runs` and `eval_cases`: RAG regression/eval results
- `audit_events`: security-sensitive action log

## Ingestion design

Core accepts `NormalizedDocument` batches. The contract is intentionally simple:

- stable `external_id`
- source-relative `path`
- normalized `source_type`
- redacted `content`
- deterministic `content_hash`
- bounded `metadata`
- optional size and modification time

Indexer behavior:

```text
new external_id
  -> create document
  -> parse/chunk/embed
  -> create chunks

same external_id + same hash
  -> skip unchanged
  -> no duplicate chunks

same external_id + changed hash
  -> prepare new chunks first
  -> update document
  -> replace old chunks safely

bad document
  -> per-document error
  -> batch continues
```

This idempotency is the difference between a sync system and a duplication generator with a logo.

## Chunking and metadata

Core uses source-aware chunking:

- logs -> log windows
- code -> code/function-ish chunks when parsable, safe fallback otherwise
- markdown/runbooks -> heading sections
- incidents -> structured report sections
- deploy history -> deploy records
- patches/diffs -> deploy/change evidence
- config/API docs -> structured text/config chunks

Metadata matters more than hype. Retrieval quality depends heavily on service names, endpoints, deploy hashes, timestamps, paths, source types, trace/request IDs, and incident dates.

## Retrieval pipeline

Core uses hybrid retrieval:

```text
query
  -> query/entity analysis
  -> vector search with pgvector
  -> lexical search with Postgres full-text
  -> metadata fusion and boosts
  -> optional reranking
  -> evidence packing
  -> redaction and prompt-injection marking
  -> citation builder
```

Why hybrid retrieval? Incident questions contain exact operational facts: deploy hashes, endpoints, error codes, service names, and timestamps. Pure vector search is not reliable enough for that.

## Investigation pipeline

Search finds evidence. Investigation reasons over evidence.

The investigation service:

- classifies task type
- extracts entities such as service, endpoint, deploy hash, symptom, time window
- retrieves and packs evidence
- builds timeline events
- generates hypotheses
- selects likely root cause
- computes confidence
- returns missing-data warnings
- includes citations and evidence references

Supported task styles include latency, error-rate, deploy regression, timeout, previous incident lookup, and generic incident questions.

Core is intentionally honest when evidence is weak. A weak fixture should produce an insufficient-evidence response, not a hallucinated executive summary wearing a tie.

## Workflow runtime

The workflow layer persists run status and events.

Statuses include pending/queued/running/waiting for approval/completed/failed style states depending on runtime mode and execution result.

Events include node start/completion/failure/retry markers. Risky actions, such as external issue creation, remain approval-gated.

Local/dev can run inline. Production-style mode uses a worker and Redis-backed queue.

## Security design

Core treats retrieved data as untrusted evidence, not instructions.

Security controls include:

- JWT auth
- bcrypt password hashing
- explicit admin bootstrap
- project RBAC
- audit events
- source config secret rejection
- redaction
- prompt-injection detection
- output sanitization
- request and batch limits
- production startup validation
- disabled production local ingest
- allowed-root path policy for local/dev ingest
- protected metrics by default

## Observability and readiness

Core exposes:

- `/health`: lightweight liveness
- `/ready`: DB, pgvector, tables/columns, migration readiness
- `/v1/capabilities`: API contract/features/limits/endpoints
- `/v1/metrics/summary`: structured runtime metrics
- `/metrics`: Prometheus-compatible path when configured

Readiness is intentionally stricter than liveness. A process being alive is not the same as the system being useful. Software keeps trying to pretend otherwise.

## Deployment modes

Recommended path:

1. Local development with Docker Compose or direct API/worker.
2. Budget-safe EC2 demo stack for public demo.
3. Managed AWS deployment with ECS/RDS/Redis/ALB when budget and operational needs justify it.

The EC2 demo stack is public-demo friendly and budget controlled. The ECS/RDS path is more production-shaped but costs more and should not be left running casually.
