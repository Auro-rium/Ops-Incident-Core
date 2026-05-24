# Collector to Core Contract

This document describes the API contract between `Ops-Incident-Collector` and `Ops-Incident-Core`.

The short version:

```text
Collector reads and normalizes data.
Core validates, stores, indexes, retrieves, and investigates.
```

Collector must not diagnose incidents, call LLMs, create embeddings, or replace Core. Core must not crawl arbitrary production folders. Boundaries exist because otherwise systems slowly mutate into a swamp with YAML.

## Capability discovery

Core exposes:

```http
GET /v1/capabilities
```

The response includes version, feature flags, limits, and endpoint templates. Collector should call this at startup or before sync when possible.

Example shape:

```json
{
  "version": "0.5.0",
  "features": {
    "sync_tracking": true,
    "collector_registration": true,
    "batch_ingest": true,
    "search": true,
    "investigate": true,
    "runs": true
  },
  "limits": {
    "max_documents_per_batch": 100,
    "max_document_bytes": 10485760,
    "max_batch_bytes": 52428800
  },
  "endpoints": {
    "register_collector": "/v1/projects/{project_id}/collectors/register",
    "register_source": "/v1/projects/{project_id}/sources",
    "create_sync": "/v1/sources/{source_id}/syncs/start",
    "batch_upload": "/v1/sources/{source_id}/documents/batch",
    "update_sync": "/v1/sources/{source_id}/syncs/{sync_id}/finish"
  }
}
```

## Authentication

Core uses bearer authentication:

```http
Authorization: Bearer <token>
```

For Azure deployment, store the Collector token in Key Vault and inject it into the Collector Container App. Use scoped service credentials and rotate them. Do not put raw secrets in source configs.

## Source registration

Collector or an operator creates a source:

```http
POST /v1/projects/{project_id}/sources
```

Payload:

```json
{
  "name": "azure-demo-source",
  "source_type": "filesystem",
  "sync_mode": "manual",
  "config": {
    "description": "safe demo fixture"
  }
}
```

Core treats source creation as idempotent by project/name/source type in the current integration path, so repeat Collector runs can reuse the same source and preserve unchanged-document skipping.

## Collector registration

Collector registers itself before sync:

```http
POST /v1/projects/{project_id}/collectors/register
```

Payload:

```json
{
  "name": "azure-demo-collector",
  "environment": "azure-demo",
  "version": "0.1.0"
}
```

Response:

```json
{
  "collector_id": "uuid",
  "status": "active"
}
```

Collector must pass `collector_id` to sync start and document batch upload.

## Start sync

```http
POST /v1/sources/{source_id}/syncs/start
```

Payload:

```json
{
  "collector_id": "uuid",
  "diagnostics": {
    "total_files_seen": 9,
    "files_skipped": 3
  }
}
```

Response:

```json
{
  "sync_id": "uuid",
  "status": "running"
}
```

## Batch upload

```http
POST /v1/sources/{source_id}/documents/batch
```

Payload:

```json
{
  "collector_version": "0.1.0",
  "schema_version": "incidentops.normalized_document.v1",
  "core_api_version": "v1",
  "sync_id": "uuid",
  "collector_id": "uuid",
  "documents": [
    {
      "external_id": "logs/app.log",
      "path": "logs/app.log",
      "source_type": "logs",
      "content": "redacted text content",
      "content_hash": "sha256hex",
      "metadata": {
        "service_name": "orders",
        "endpoint": "/v1/orders",
        "deploy_hash": "abcdef1234567890"
      },
      "size_bytes": 1234,
      "modified_at": "2026-05-21T10:00:00Z"
    }
  ]
}
```

Core stores protocol metadata in sync diagnostics:

- `collector_version`
- `schema_version`
- `core_api_version`

Batch response includes counts, diagnostics, coverage, warnings, embedding backend, and per-document errors.

## NormalizedDocument rules

Required fields:

- `external_id`: stable source-local identifier
- `path`: safe source-relative path
- `source_type`: normalized type such as logs/code/deploy/incident/runbook/api_doc/config/unknown_text
- `content`: redacted text content
- `content_hash`: SHA-256 digest
- `metadata`: JSON object

Rules:

- no absolute paths unless explicitly supported
- no path traversal
- no raw secrets
- metadata must be bounded and JSON-serializable
- empty/binary/oversized docs are skipped or returned as per-document errors

## Source type mapping

Core known source types:

- `logs`
- `code`
- `deploy`
- `incident`
- `runbook`
- `api_doc`
- `config`
- `unknown_text`

Collector may map local source types such as `logs_folder`, `git_local`, `deploy_history`, `incident_report`, and `runbooks` into Core-compatible source types.

## Finish sync

```http
POST /v1/sources/{source_id}/syncs/{sync_id}/finish
```

Core accepts statuses:

- `success`
- `partial_success`
- `failed`
- `cancelled`

Collector should map its internal state as:

```text
no failed uploads -> success
failed uploads -> partial_success
fatal sync exception -> failed
cancelled -> cancelled
```

Payload:

```json
{
  "status": "success",
  "diagnostics": {
    "files_seen": 9,
    "files_skipped": 3,
    "documents_synced": 6,
    "failed_uploads": 0,
    "retry_attempted": 0,
    "retry_succeeded": 0
  },
  "coverage": {
    "has_logs": true,
    "has_code": true,
    "has_deploys": true,
    "has_incidents": true,
    "has_runbooks": true
  }
}
```

Core may convert `success` to `partial_success` if parser/indexing errors occurred during batch ingest.

## Idempotency guarantees

Core guarantees:

- repeated sync of unchanged docs skips unchanged documents
- changed content hash updates the document and replaces old chunks
- bad documents do not fail the whole batch
- no duplicate chunks for unchanged retries

Validated integration behavior:

```text
first sync: files seen 9, documents synced 6, search results 8
repeat sync: unchanged documents skipped
changed log file: one document updated, no duplicate chunks
```

## Search and investigate after sync

After sync, clients can call:

```http
POST /v1/search
POST /v1/investigate
```

Search returns evidence hits with citations. Investigate returns task type, evidence, citations, confidence, missing data, and root-cause summary. If evidence is weak, Core should say so instead of inventing certainty.

## Failure handling

Collector should retry retryable API errors and persist failed uploads locally. Core returns per-document errors for parse, validation, or indexing failures.

Do not echo raw document content or secrets in errors, logs, diagnostics, or audit metadata.
