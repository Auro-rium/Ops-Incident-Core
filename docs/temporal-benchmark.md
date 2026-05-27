# Temporal Benchmark Status

Temporal is the current large-repository benchmark for IncidentOps.

Target repository:

```text
https://github.com/temporalio/temporal
```

Temporal is a useful stress target because it is a large distributed-systems backend repository with Go code, proto/API definitions, service packages, persistence layers, configuration, docs, and operational complexity.

## First Azure Run Result

The first Azure Temporal run proved that the cloud product loop worked, but it did not prove deep Temporal code intelligence.

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

Search worked technically:

```text
/v1/search calls: 10
success: 10
failed: 0
results per query: 8
avg search latency: about 1.58s
p95 search latency: about 1.60s
```

Retrieval quality was only partially useful:

- README and documentation questions worked.
- Real code questions were weak because Go source was not deeply indexed in that run.
- Runtime investigation questions correctly returned cautious or insufficient-evidence responses when runtime logs, deployment history, traces, metrics, and incident records were missing.

## Why It Was Shallow

Temporal is mostly Go/proto. The first benchmark skipped or failed most of that evidence:

```text
.go skipped: 1283
.proto skipped: 56
```

That means the result was not “Temporal fully understood.” It was “the Azure loop works, and the ingestion/chunking bottleneck is now visible.” That is useful because the next engineering problem is specific instead of mystical.

## Current Code Direction

Core contains parser/chunker paths for Go and proto-style declaration parsing:

- Go files: declaration-oriented chunks for functions and types, with fallback file chunks.
- Proto files: service/message/enum chunks, with fallback API-doc chunks.
- Chunk metadata carries language, symbol, kind, path, section title, and line ranges where available.

Collector supports `.go` and `.proto` in its default extension set and extracts deterministic metadata such as package, functions/types, proto services/messages, language, module path, headings, endpoints, config keys, commits, and operational hints.

The next proof is not whether code exists. The next proof is whether the deployed Azure benchmark numbers improve.

## Rerun Goal

Rerun Temporal after deploying the current Go/proto support through Azure CI/CD.

Improvement targets:

```text
files_seen: 1500
documents_normalized: 500+
chunks_created: 1000+
.go skipped: near 0 for included paths
.proto skipped: near 0 for included paths
parser_errors / skipped_invalid: far below 85
sync_status: success or explained partial_success
```

These are targets, not guarantees. They define the bar for calling Temporal a real retrieval proof.

## Required Temporal Queries

Use repo-grounded queries first:

```text
Where is the history service implemented?
Where are matching service responsibilities implemented?
Where is namespace management implemented?
Where are persistence stores or database-related components defined?
Where are proto API definitions for workflows or history located?
Which parts of the repo are relevant to investigating workflow task latency?
What evidence is missing before IncidentOps could diagnose a real runtime issue in Temporal?
```

Expected behavior:

- history/matching/namespace/persistence questions should return relevant Go/proto paths.
- proto questions should return `.proto` evidence.
- workflow task latency questions should return relevant code areas plus missing-runtime-evidence warnings.
- runtime diagnosis should remain cautious unless logs, deploy records, traces, metrics, and incident records are present.

## Benchmark Report Requirements

The rerun report should include before/after metrics:

| Metric | First run | Rerun |
|---|---:|---:|
| Files seen | 1500 | |
| Files skipped | 1413 | |
| `.go` skipped | 1283 | |
| `.proto` skipped | 56 | |
| Documents normalized | 87 | |
| Documents received by Core | 87 | |
| Chunks created | 12 | |
| Parser errors / skipped invalid | 85 | |
| Redaction count | 28 | |
| Sync status | partial_success | |
| Search success rate | 10/10 | |
| Search avg latency | about 1.58s | |
| Investigation citation count | | |
| Repeat sync skipped unchanged | | |
| Changed-file update | | |
| Duplicate chunks after update | | |

## Definition of Success

The Temporal benchmark is successful when:

- Go/proto evidence is ingested instead of mostly skipped.
- Source-aware chunks are created at useful scale.
- Search returns relevant Go/proto paths for architecture questions.
- Investigations cite indexed evidence and preserve missing-data warnings.
- Repeat sync and changed-file update still work.
- Duplicate chunks remain 0.
- The benchmark report is published with numbers.

## Definition of Failure

The benchmark is not successful if:

- chunks remain near 12
- Go/proto remains mostly skipped
- parser errors remain near 85 without explanation
- search returns only README/docs for code questions
- investigation overclaims runtime cause without runtime evidence
- reports hide partial success

Partial success is allowed. Hidden failure is not.
