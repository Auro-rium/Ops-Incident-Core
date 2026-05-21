# IncidentOps Core

IncidentOps Core is a production-style, collector-first RAG backend for incident investigation. It turns engineering evidence such as logs, code, deploy history, runbooks, API docs, database notes, and previous incident reports into searchable, cited evidence for backend and SRE teams.

The system is not a generic PDF chatbot. It is an incident investigation backend designed around source-aware ingestion, hybrid retrieval, confidence scoring, missing-data warnings, workflow runs, auditability, and deployment discipline. Fancy words, yes, but this time they map to actual running services instead of decorative README fog.

## What it does

IncidentOps Core answers operational questions such as:

```text
Why did /checkout latency spike after the last deploy?
What changed before the timeout errors started?
Which logs, deploys, docs, and previous incidents support this hypothesis?
What evidence is missing before we can trust the root-cause claim?
```

It returns:

- cited evidence
- likely root-cause hypothesis
- confidence and confidence reasons
- missing data and unknowns
- affected services
- timeline and hypotheses
- workflow run events
- report/issue draft state behind approval gates

## Repository role

This repository is the **Core Backend**.

The full IncidentOps system is split into three repositories:

```text
Ops-Incident-Core        FastAPI backend, workers, database, retrieval, investigation
Ops-Incident-Collector   deterministic edge collector and Core sync client
Ops-Incident-frontend    operator console UI
```

Core owns:

- API contract
- authentication and RBAC
- source and collector registry
- sync lifecycle
- normalized document batch ingestion
- indexing and chunk storage
- hybrid retrieval
- investigation responses
- workflow runs and approvals
- evals, metrics, readiness, audit events
- production deployment assets

Collector owns local data access, filtering, redaction, normalization, and upload. The frontend owns the operator experience. Splitting them is not aesthetic minimalism. It prevents the backend from becoming a junk drawer with Docker Compose wallpaper.

## Core architecture

```text
Collector or local/dev ingest
  -> NormalizedDocument[]
  -> validation and idempotent indexer
  -> documents + chunks in Postgres
  -> pgvector + full-text retrieval
  -> evidence packing + citations
  -> investigation service
  -> workflow runs, events, approvals, evals, metrics
```

The production ingestion path is Collector-first:

1. Create a project.
2. Create/register a source.
3. Register a collector.
4. Start a source sync.
5. Send normalized document batches.
6. Finish the sync.
7. Search or investigate over indexed evidence.

Core exposes `/v1/capabilities` so Collector and tooling can discover supported features, limits, and endpoint paths.

## Normalized document contract

Collector sends documents shaped like:

```json
{
  "external_id": "logs/app.log",
  "path": "logs/app.log",
  "source_type": "logs",
  "content": "timestamped log content...",
  "content_hash": "sha256...",
  "metadata": {
    "service_name": "orders",
    "endpoint": "/v1/orders",
    "deploy_hash": "abcdef1234567890"
  },
  "size_bytes": 1234,
  "modified_at": "2026-05-21T10:00:00Z"
}
```

Batch ingestion is idempotent by source and external identity:

- same external ID and same content hash -> skipped unchanged
- same external ID and new content hash -> document updated and old chunks replaced
- bad document -> per-document error, not whole batch failure
- repeated sync -> no duplicate chunks

## Retrieval and investigation

Core uses hybrid retrieval instead of pretending embeddings alone can remember deploy hashes, endpoints, and error codes like a responsible adult.

Retrieval combines:

- pgvector semantic search
- Postgres full-text search
- metadata filters and boosts
- optional reranking
- evidence packing
- citation building
- secret redaction and prompt-injection marking

Investigation then performs incident-specific reasoning over evidence:

- task classification
- entity extraction
- evidence retrieval
- timeline construction
- hypothesis generation
- confidence scoring
- missing-data detection
- cited answer generation

When evidence is weak, Core does not fake certainty. It returns an insufficient-evidence root cause with citations and missing-data guidance.

## Security model

Core includes:

- JWT authentication
- bcrypt password hashing
- explicit admin bootstrap
- project-scoped RBAC
- audit events
- source config secret rejection
- redaction and output sanitization
- prompt-injection inspection for retrieved content
- request and batch limits
- Redis-backed rate limit/queue paths for production
- local folder ingest disabled by default in production
- canonical allowed-root validation for local/dev ingest

Production must not rely on demo-mode bypasses or runtime `create_all` schema creation.

## Runtime services

Core can run as separate API and worker processes.

```text
core-api       FastAPI API
core-worker    background workflow/eval worker
postgres       PostgreSQL + pgvector
redis          queue/rate-limit/runtime backing service
```

Local/development can use inline execution:

```text
WORKER_MODE=inline
JOB_QUEUE_BACKEND=inline
```

Production-style runtime should use queue mode:

```text
WORKER_MODE=queue
JOB_QUEUE_BACKEND=redis
RATE_LIMIT_BACKEND=redis
```

## Local quick start

```bash
cp .env.example .env
alembic upgrade head
python scripts/check_migrations.py
uvicorn apps.api.main:app --reload --port 8000
```

Start a worker separately when using queue mode:

```bash
python -m incidentops.worker
```

Run tests:

Production AWS deployment assets live under:

- `infra/terraform/`
- `.github/workflows/deploy-core.yml`
- `docs/aws-deployment.md`

The AWS deployment uses ECS Fargate for separate API and worker services, RDS PostgreSQL, ElastiCache Redis, ECR, Secrets Manager, an Application Load Balancer, CloudWatch logs, a migration one-off task, and `smoke_prod.py` after deploy.

Production must run Alembic migrations before service rollout and must not use SQLAlchemy `create_all`.

For a budget-safe single-instance flagship demo, see [docs/ec2-demo-deployment.md](docs/ec2-demo-deployment.md). That path runs Core, worker, Postgres pgvector, Redis, Collector, the separate `Ops-Incident-frontend` repo, and Nginx on one EC2 instance with Docker Compose and avoids RDS, ElastiCache, ALB, NAT Gateway, and ECS.

Expected EC2 sibling repo layout:

```text
~/incidentops/
  Ops-Incident-Core/
  Ops-Incident-Collector/
  Ops-Incident-frontend/
```

Deploy with:

```bash
scripts/deploy_ec2_demo.sh \
  --public-url http://YOUR_EC2_PUBLIC_DNS_OR_IP \
  --collector-repo ../Ops-Incident-Collector \
  --frontend-repo ../Ops-Incident-frontend
```

## Frontend

Run a local smoke test:

```bash
python scripts/smoke_local.py \
  --base-url http://127.0.0.1:8000 \
  --data-path tests/fixtures/basic_incident \
  --query "Why did GET /v1/orders slow down after deploy abc1234?" \
  --create-run
```

Run production-style smoke:

```bash
python scripts/smoke_prod.py \
  --base-url http://127.0.0.1:8000 \
  --email admin@incidentops.local \
  --password incidentops \
  --query "What does this tiny service evidence say?"
```

## EC2 demo deployment

The budget-safe demo stack runs the full system on one EC2 instance using Docker Compose:

```text
Nginx public on 80/443
Frontend console
Core API
Core worker
Postgres pgvector
Redis
Collector daemon
```

Expected EC2 sibling repo layout:

```text
~/incidentops/
  Ops-Incident-Core/
  Ops-Incident-Collector/
  Ops-Incident-frontend/
```

Deploy with:

```bash
scripts/deploy_ec2_demo.sh \
  --public-url http://YOUR_EC2_PUBLIC_DNS_OR_IP \
  --collector-repo ../Ops-Incident-Collector \
  --frontend-repo ../Ops-Incident-frontend
```

Smoke:

```bash
scripts/smoke_ec2_demo.sh
```

See [`docs/ec2-demo-deployment.md`](docs/ec2-demo-deployment.md) for the EC2 runbook.

## AWS managed deployment

This repository also includes deployment assets for a more production-shaped AWS path:

- ECS Fargate API service
- ECS Fargate worker service
- RDS PostgreSQL
- ElastiCache/Redis or Valkey
- ECR
- Secrets Manager
- CloudWatch logs
- ALB
- migration one-off task
- GitHub Actions CI/CD

See:

- [`docs/deployment-overview.md`](docs/deployment-overview.md)
- [`docs/aws-deployment.md`](docs/aws-deployment.md)
- [`docs/operations-runbook.md`](docs/operations-runbook.md)

## Documentation map

- [`docs/system-architecture.md`](docs/system-architecture.md): system design and data flow
- [`docs/collector-core-contract.md`](docs/collector-core-contract.md): Collector to Core API contract
- [`docs/operations-runbook.md`](docs/operations-runbook.md): operating, debugging, and demoing Core
- [`docs/deployment-overview.md`](docs/deployment-overview.md): deployment options and promotion path
- [`docs/ec2-demo-deployment.md`](docs/ec2-demo-deployment.md): one-box AWS EC2 demo deployment

## Current maturity

IncidentOps Core has been validated as a deployed AWS EC2 demo stack with Collector sync, search, investigation, citations, and workflow runs working end-to-end. It is production-style and portfolio/flagship ready. It is not yet a fully managed SaaS deployment until domain, HTTPS, managed DB, alerting, backups, and long-running CI/CD operations are completed.

That distinction matters. Overclaiming is how good engineering turns into brochure fiction.
