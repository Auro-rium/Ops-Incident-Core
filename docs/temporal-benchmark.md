# Temporal Benchmark Status

Temporal remains the current large-repo stress target for IncidentOps.

Target repository:

```text
https://github.com/temporalio/temporal
```

Temporal is useful because it is large enough to expose queueing, embedding, chunking, and ranking weaknesses instead of politely hiding them.

## Latest verified benchmark state

The latest verified Azure Temporal run, before the next clean rerun, produced this result:

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

Top skip reasons:

```text
unsupported_extension: 62
denied: 9
empty: 1
```

Most common unsupported extensions in that run:

```text
.svg: 20
.d2: 18
.sh: 7
.cql: 5
.tmpl: 4
```

## What this run proved

It proved three things:

1. Collector can walk a large real repo, normalize a large number of documents, and redact content safely.
2. The failure point was not discovery or normalization. The failure point was the Core-side ingest/runtime path.
3. The benchmark exposed architecture bugs that were worth fixing:
   - Azure OpenAI embedding throttling
   - embedding work blocking the API event loop
   - collector batch uploads sharing a human-scale request limit

## What it did not prove

It did **not** prove:

- useful Temporal-scale chunk coverage
- useful Temporal-scale code retrieval quality
- answer/investigation quality on Temporal
- Go/proto retrieval performance

Core received zero documents in that verified run, so any stronger claim would be fiction.

## Current code direction after the failed run

The current Core codebase has already moved in the right direction:

- bounded Azure embedding retry/backoff
- embedding work offloaded from the API event loop
- separate collector batch upload rate limit
- source-aware Go chunk types:
  - `go_module`
  - `go_type`
  - `go_function`
  - `go_method`
- source-aware proto chunk types:
  - `proto_preamble`
  - `proto_service`
  - `proto_rpc`
  - `proto_message`
  - `proto_enum`
- retrieval intent routing and diagnostics
- direct-evidence answer fast paths

Those code changes are deployed, but the benchmark report should not be rewritten as a success story until the rerun numbers exist.

## Required rerun bar

The next Temporal rerun should publish:

| Metric | Latest verified run | Next rerun |
|---|---:|---:|
| Files seen | 1500 | |
| Files skipped | 72 | |
| Documents normalized | 1428 | |
| Documents received by Core | 0 | |
| Chunks created | 0 | |
| Failed uploads | 2856 | |
| Retry attempted | 1428 | |
| Retry succeeded | 0 | |
| Redaction count | 71 | |
| Sync status | partial_success | |
| Search result count | | |
| Answer/investigation citation count | | |
| Search latency avg/p95 | | |
| Answer latency avg/p95 | | |
| Investigation latency avg/p95 | | |
| Prompt/completion/total tokens | | |

## Definition of success

The Temporal benchmark is only successful when all of these are true:

- Core accepts documents and creates chunks
- Go/proto paths appear in top results for code-location questions
- runtime questions stay cautious when runtime evidence is missing
- repeated sync skips unchanged documents
- changed-file update replaces old chunks without duplicates
- live latency and token numbers are published

Until then, Temporal remains a failure-driven benchmark. That is still useful, but it is not a success proof.
