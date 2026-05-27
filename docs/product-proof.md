# IncidentOps Product Proof

This document defines what counts as product proof for IncidentOps. It is intentionally stricter than “the app deployed.” Cloud containers being alive is not product value; it is just a server bill with better posture.

## Product Claim

IncidentOps helps backend teams turn scattered engineering evidence into cited, queryable incident context.

A useful proof run should show:

```text
real repo or evidence bundle
  -> Collector scan/redaction/normalization
  -> Core batch ingest
  -> PostgreSQL documents/chunks + pgvector
  -> Azure OpenAI embeddings/synthesis
  -> readiness report
  -> cited search
  -> cited investigation
  -> Core MCP tool call
  -> metrics table
```

## Minimum Proof Metrics

Every serious benchmark should publish this table:

| Metric | Value |
|---|---:|
| Repo / evidence bundle | |
| Commit SHA | |
| Files seen | |
| Files skipped | |
| Documents normalized | |
| Documents received by Core | |
| Chunks created | |
| Parser errors / skipped invalid | |
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

## Current Known Proof

The Azure production-style path has proven:

- Azure Container Apps deployment for Core API, worker, Collector, frontend, and MCP server
- Azure PostgreSQL + pgvector backing store
- Azure Redis queue/rate-limit path
- Azure OpenAI embeddings and answer synthesis in deployed mode
- Core health/readiness
- Collector sync into Core
- readiness endpoint
- search, answer, and investigation calls
- internal Core MCP app deployment
- GitHub Actions CI/CD deployment flow

A 10-question cloud run succeeded technically:

```text
HTTP calls: 36
Succeeded: 36
Failed: 0
Search calls: 10
Answer calls: 10
Investigate calls: 10
LLM calls: 10
Prompt tokens: 15,674
Completion tokens: 14,688
Total tokens: 30,362
Search avg latency: ~1.58s
Answer avg latency: ~21.8s
Investigate avg latency: ~1.63s
```

## Current Known Bottleneck

The Temporal stress run exposed weak Go/proto coverage:

```text
files_seen: 1500
files_skipped: 1413
documents_normalized: 87
documents_received_by_core: 87
chunks_created: 12
parser_errors / skipped_invalid: 85
redaction_count: 28
sync_status: partial_success
```

This means the cloud system worked, but Temporal repo intelligence was shallow. The next product-quality milestone is deeper Go/proto indexing and retrieval validation.

## What Not To Claim Yet

Do not claim:

- “fully production-grade AI SRE replacement”
- “Temporal fully understood”
- “Azure AI Search production retrieval” unless Azure AI Search is implemented and benchmarked
- “MCP public product surface” unless the protected client path is validated
- “root cause quality” without runtime logs, deploy history, traces, metrics, and incident reports

Claim this instead:

> IncidentOps is an Azure-deployed, collector-first incident investigation RAG system with real repo ingestion, Azure OpenAI embeddings/synthesis, PostgreSQL/pgvector retrieval, readiness reports, cited investigation, workflow runs, and Core MCP tools. Current stress testing exposed the next retrieval-quality bottleneck: deeper Go/proto support for large backend repositories like Temporal.

## Definition of Done for the Next Proof

The next proof is complete when the Temporal rerun shows:

- `.go` and `.proto` files are no longer massively skipped
- documents normalized increases substantially
- chunks created increases substantially beyond 12
- parser/skipped-invalid count drops substantially below 85
- history/matching/namespace/persistence queries return relevant Go/proto paths
- missing-evidence questions remain honest
- repeated sync skips unchanged docs
- changed-file update passes
- duplicate chunks remain 0
- reports include before/after metrics

Target, not promise:

```text
documents_normalized: 500+
chunks_created: 1000+
.go skipped: near 0 for included paths
.proto skipped: near 0 for included paths
```

Numbers beat vibes. Very rude of reality, but useful.
