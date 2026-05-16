# Architecture

> Back to docs index: [docs/README.md](./README.md)

IncidentOps Agent is split into ingestion, retrieval, investigation, agent workflow, security, evals, observability, and frontend surfaces. Retrieval stays deterministic and metadata-driven. Investigation turns evidence into timelines and hypotheses. The run workflow persists progress and approvals for inspectability.

## Container View (C4-style)

```mermaid
graph TD
  U[User] --> W[Web App\napps/web]
  W --> A[API Service\napps/api]
  A --> DB[(Postgres + pgvector)]
  A --> R[(Redis Queue / Rate Limit)]
  A --> M[(Metrics/Tracing Backend)]

  R --> WK[Worker Service\nincidentops/worker]
  WK --> DB
  WK --> LLM[LLM Provider]
  WK --> M
  A --> LLM
```

## Runtime Split

Production runtime separates API request handling from long-running work:

```text
API request
  -> validate auth/RBAC/limits
  -> create persisted run/eval record
  -> enqueue job
  -> return run_id/eval_run_id

Worker
  -> dequeue job
  -> execute workflow/eval
  -> persist events/status/results
  -> record audit and metrics
```

Local development can run inline execution for fast smoke tests. Staging and production should use `WORKER_MODE=queue` with `JOB_QUEUE_BACKEND=redis`.

## Capability Data Flows

### 1) Ingestion

- Input: source/collector/sync or local ingest payload.
- Key modules: `incidentops/ingestion/pipeline.py`, parsers under `incidentops/ingestion/parsers/`, chunking under `incidentops/ingestion/chunking/`.
- Persistence: normalized/chunked records and embeddings persisted through DB models/repositories.
- Output: searchable evidence chunks with metadata and redaction-safe diagnostics.

### 2) Retrieval

- Input: user query + project scope.
- Key modules: `incidentops/retrieval/hybrid_search.py`, `vector_search.py`, `lexical_search.py`, `reranker.py`, `evidence_packer.py`, `citation_builder.py`.
- Persistence touchpoints: vector/metadata lookup over evidence corpus in Postgres/pgvector.
- Output: deterministic evidence pack with ranked results and citation artifacts.

### 3) Investigation

- Input: investigation request + evidence candidates.
- Key modules: `incidentops/investigation/service.py`, timeline/classifier/analyzer modules.
- Persistence: investigation run state, timeline artifacts, and outputs.
- Output: timeline, hypotheses, likely root-cause candidates.

### 4) Agent Workflow

- Input: workflow run request.
- Key modules: `incidentops/agent/graph.py`, `nodes.py`, `state.py`, `service.py`, `events.py`.
- Behavior: deterministic node orchestration over existing classifiers/retrieval/analyzers.
- Persistence: node lifecycle events (`start/completion/failure/retry`), status, approvals, final report.

Workflow execution is bounded by node timeout/retry and run-level timeout settings.

## End-to-End Sequence (Queue Mode Investigation)

```mermaid
sequenceDiagram
  participant C as Client
  participant API as API Service
  participant DB as Postgres
  participant Q as Redis Queue
  participant W as Worker

  C->>API: POST /v1/investigate
  API->>API: auth + RBAC + request validation
  API->>DB: create run record (queued)
  API->>Q: enqueue job(run_id)
  API-->>C: 202 + run_id

  W->>Q: dequeue job
  W->>DB: mark run in_progress
  W->>W: execute deterministic workflow nodes
  W->>DB: persist node events + outputs
  W->>DB: mark run completed/failed
```

## Failure Modes and Recovery

| Failure mode | Detection signal | Typical impact | Recovery action |
|---|---|---|---|
| Redis queue unavailable | queue enqueue/dequeue failures, run backlog | new runs remain queued or fail to enqueue | restore Redis connectivity, restart workers if needed, replay queued runs |
| DB migration drift | `/ready` fails migration check | API not production-ready | run `alembic upgrade head`, verify `python scripts/check_migrations.py` |
| LLM timeout | workflow node timeout events, increased run latency | partial/failed report generation | tune node/run timeout, retry policy, provider connectivity |
| Malformed ingest document | per-document ingest errors | partial ingestion | fix offending document/metadata, re-submit batch |
| Approval gate stall | run remains awaiting approval beyond SLA | delayed incident closure | review pending approvals, approve/reject to unblock run |

