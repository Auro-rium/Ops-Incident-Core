# Collector Protocol · [Docs Hub](./README.md)

IncidentOps Core Backend is Collector-first. Collectors read company-controlled data sources, normalize documents, and send them to the Core API. The Core does not need direct access to repos, Slack, observability tools, or company file shares.

## Flow

1. Create a source with `POST /v1/projects/{project_id}/sources`.
2. Register a collector with `POST /v1/projects/{project_id}/collectors/register`.
3. Start a sync with `POST /v1/sources/{source_id}/syncs/start`.
4. Send one or more batches to `POST /v1/sources/{source_id}/documents/batch`.
5. Finish the sync with `POST /v1/sources/{source_id}/syncs/{sync_id}/finish`.
6. Use `/v1/search`, `/v1/answer`, or `/v1/investigate`.

## Normalized Document

```json
{
  "external_id": "logs/orders-2026-05-05.log",
  "path": "logs/orders-2026-05-05.log",
  "source_type": "logs",
  "content": "...",
  "content_hash": "sha256 hex digest",
  "metadata": {
    "service_name": "orders",
    "environment": "prod"
  },
  "size_bytes": 12345,
  "modified_at": "2026-05-05T10:00:00Z"
}
```

`external_id` must be stable within the source. `path` should be source-relative. Path traversal, null bytes, empty content, invalid hashes, oversized metadata, and binary-looking content are rejected as per-document errors.

Supported source types are `logs`, `code`, `deploy`, `incident`, `runbook`, `api_doc`, `config`, and `unknown_text`. Unknown source types are mapped safely from the path where possible.

## Idempotency

Document identity is `(project_id, source_id, external_id)`.

- New `external_id`: create the document and chunks.
- Same `external_id` and same `content_hash`: skip unchanged.
- Same `external_id` and changed `content_hash`: parse and embed the new chunks first, then replace old chunks inside a per-document transaction.
- One bad document: return an error for that document and continue the batch.
- Repeated batch retry: unchanged documents skip without duplicate chunks.

## Limits

Configured limits:

- `MAX_DOCUMENTS_PER_BATCH`
- `MAX_DOCUMENT_BYTES`
- `MAX_BATCH_BYTES`
- `MAX_CHUNKS_PER_DOCUMENT`
- `MAX_METADATA_BYTES`
- `MAX_EXTERNAL_ID_LENGTH`
- `MAX_PATH_LENGTH`

A batch over count or byte limits is rejected. An individual oversized or invalid document is returned in `errors` and does not block valid documents in the same batch.

## Diagnostics

Batch responses include:

- `received`
- `created`
- `updated`
- `skipped_unchanged`
- `skipped_invalid`
- `chunks_created`
- `errors`
- `diagnostics`
- `coverage`
- `embedding_backend`
- `warnings`

Sync diagnostics aggregate safe counters only. They never include document content.

## Coverage

Coverage reports whether the sync saw usable logs, code, deploy metadata, previous incidents, runbooks, and API docs. Missing categories produce warnings such as:

- No logs found. Runtime symptom analysis may be weak.
- No code found. Code-path and diff reasoning will be limited.
- No deploy history found. Deploy-regression investigations may be weak.
- No previous incidents found. Similar-incident lookup unavailable.
- No runbooks or API docs found. Ownership and remediation guidance may be weak.

Coverage warnings are advisory, not failures.

## Local Ingest

`POST /v1/projects/{project_id}/ingest` remains for local development and smoke tests. It reads a server-visible folder, converts files into `NormalizedDocument` objects, and uses the same central indexer as collector batch ingest.
