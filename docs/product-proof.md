# IncidentOps Product Proof

This document defines what counts as product proof for IncidentOps and what remains only partially proven.

The standard is stricter than “the containers are up.” A useful proof has to show that Collector, Core, retrieval, and answer behavior produce inspectable engineering evidence with honest limitations.

## What counts as proof

A serious proof run should show:

```text
real repo or evidence bundle
  -> Collector scan, redaction, normalization
  -> Core batch ingest
  -> PostgreSQL documents/chunks + pgvector
  -> readiness report
  -> cited search
  -> cited answer or investigation
  -> Core MCP tool call
  -> metrics table
```

## Minimum metrics to publish

Every benchmark or demo report should publish this table:

| Metric | Value |
|---|---:|
| Repo / evidence bundle | |
| Commit SHA | |
| Files seen | |
| Files skipped | |
| Documents normalized | |
| Documents received by Core | |
| Chunks created | |
| Parser error count | |
| Top parser error reasons | |
| Redaction count | |
| Sync status | |
| Embedding backend | |
| Retrieval backend | |
| LLM provider | |
| Readiness score | |
| Search calls | |
| Search success rate | |
| Search latency avg/p95 | |
| Answer / investigation calls | |
| Citation count | |
| Prompt tokens | |
| Completion tokens | |
| Total tokens | |
| MCP tools validated | |
| Repeat sync skipped unchanged | |
| Changed-file update result | |
| Duplicate chunks after update | |

If a metric is unavailable, say so explicitly. Do not backfill vibes into an empty cell.

## What is actually proven now

Current verified proof points:

- Core Azure deployment runs through `.github/workflows/deploy-azure.yml`.
- Collector Azure validation/dispatch runs through `.github/workflows/deploy-collector.yml`.
- Core health/readiness, login, readiness, search, investigate, MCP token wiring, and Azure smoke are working on the current Azure stack.
- Runtime status reports production cloud mode:
  - Azure OpenAI chat/embeddings
  - PostgreSQL/pgvector retrieval
  - Redis worker/rate-limit backend
  - no local fallback in production mode
- Core now exposes purge endpoints, runtime status, capabilities, readiness, parser failure taxonomy, retrieval diagnostics, and direct-evidence answer fast paths for simple code/config/API lookups.

Latest verified Core deployment runs at the time of this update:

- `26735683633` — success
- `26735351384` — success
- `26734478346` — success

## What is not proven yet

The large-repo benchmark story is not done. The latest verified Temporal-scale run was a failure report, not a success proof:

```text
files_seen: 1500
files_skipped: 72
documents_normalized: 1428
documents_received_by_core: 0
chunks_created: 0
failed_uploads: 2856
retry_attempted: 1428
retry_succeeded: 0
redaction_count: 71
sync_status: partial_success
```

That run exposed three real flaws:

1. Azure OpenAI embedding throttling needed bounded retry/backoff.
2. Embedding work and retry sleeps were happening on the API event loop, which made the API unhealthy during large syncs.
3. Collector batch uploads were sharing a human-scale request limit and were rejected with `429 Too Many Requests`.

The first two fixes are live. The collector-batch rate-limit separation is deployed and needs a clean rerun before new benchmark numbers belong in a proof document.

## What not to claim

Do not claim:

- broad large-repo retrieval quality
- deep Go/proto understanding across Temporal-scale repos
- root-cause quality for runtime incidents without logs/deploys/incidents
- Azure AI Search support
- public MCP product access without an authenticated client path

Claim this instead:

> IncidentOps is an Azure-deployed, collector-first engineering-evidence backend with real cloud deployment, readiness, cited search, investigation, workflow runtime, and Core MCP integration. The current engineering work is focused on raising retrieval quality and latency under large-repo ingest pressure.

## Definition of done for the next proof

The next proof should only be called complete when a fresh large-repo rerun shows:

- documents actually accepted by Core
- chunks created at useful scale
- parser failures typed by reason
- code-location queries returning code/proto over README noise
- repeated sync skipping unchanged docs
- changed-file update replacing chunks without duplicates
- latency and token metrics published from live Azure

Until then, the honest status is:

- cloud deployment: proven
- product runtime loop: proven
- large-repo RAG quality: still under active hardening
