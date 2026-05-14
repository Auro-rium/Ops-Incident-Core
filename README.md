# IncidentOps Agent

IncidentOps Agent is a production-style incident investigation copilot for backend and SRE teams. It is designed for bring-your-own incident data: logs, docs, code, incident reports, and deploy metadata from arbitrary project folders.

## Bring Your Own Data

Expected folder types:
- application and gateway logs
- service code and patches
- deploy history or release metadata
- incident reports or postmortems
- runbooks and API docs

Supported files:
- `.md`
- `.txt`
- `.log`
- `.json`
- `.yaml`
- `.yml`
- `.py`
- `.patch`
- `.diff`

What good input looks like:
- timestamped logs with service names or request paths
- deploy history with hashes, times, and changed files
- code or diffs for the affected service
- prior incident writeups and runbooks

What weak input looks like:
- docs only, without logs or deploy context
- logs without timestamps
- deploy questions without deploy metadata
- folders dominated by unsupported binaries or oversized dumps

Low confidence means the system found some relevant evidence but key context is missing or inconsistent. In that case it will return the best evidence it has, confidence reasons, and missing data needed to investigate further.

## Workflow

1. Start the backend.
2. Create a project.
3. Ingest a server-visible folder such as `/path/to/logs-and-docs`.
4. Ask an incident question.
5. Inspect cited evidence, investigation output, and optional workflow run events.

Unsupported files are skipped safely. Oversized files are skipped with a logged reason.

## Main capabilities

- metadata-aware ingestion and hybrid retrieval
- structured incident investigation with citations
- workflow runs with persisted events and approval gates
- JWT auth, RBAC, redaction, prompt-injection inspection, output sanitization
- persisted eval runs with custom case files

## Quick start

```bash
cp .env.example .env
alembic upgrade head
uvicorn apps.api.main:app --reload --port 8000
```

Then use the frontend or CLI:

```bash
python scripts/smoke_local.py \
  --data-path /path/to/project/data \
  --query "Why did latency increase after the last deploy?" \
  --base-url http://127.0.0.1:8000
```

Inspect a folder before ingesting:

```bash
python scripts/inspect_folder.py --data-path /path/to/project/data
```

## Docker

Run the full stack:

```bash
docker compose up --build
```

Then open:

```txt
http://127.0.0.1:3000
```

The compose file mounts `${INGEST_ROOT:-.}` into the API container as `/workspace` read-only. When the backend runs in Docker, ingest paths must be container-visible paths such as:

```txt
/workspace/tests/fixtures/basic_incident
```

To point Docker at a different host folder tree, start compose with `INGEST_ROOT=/path/to/data-root`.

Run migrations before using a fresh database:

```bash
alembic upgrade head
python scripts/check_migrations.py
```

## Frontend

The frontend lets you:
- enter API base URL
- create a project
- ingest a server-visible path
- enter a custom query
- inspect evidence
- inspect investigation output
- inspect workflow run events

## Evals

Run custom eval cases:

```bash
python incidentops/eval/runner.py \
  --project-id PROJECT_ID \
  --cases eval/custom_cases.jsonl \
  --base-url http://127.0.0.1:8000 \
  --email admin@incidentops.local \
  --password incidentops
```

Case format:

```json
{
  "id": "case_001",
  "question": "Why did latency increase after the deploy?",
  "expected_documents": ["deploy-history.json"],
  "expected_terms": ["deploy", "latency"],
  "forbidden_terms": ["database outage"]
}
```

## Tests and fixtures

Tiny fixtures live under `tests/fixtures/basic_incident/`. They are only for tests and smoke coverage. The main product flow does not depend on `demo_data`.

## Database and readiness

The backend is production-deployable from Alembic migrations. Runtime schema creation is disabled by default.

Environment flags:
- `APP_ENV=local|development|staging|production`
- `DB_CREATE_ALL=false`
- `DB_REQUIRE_MIGRATIONS=true`

`DB_CREATE_ALL=true` is only honored in `local` or `development`. It is ignored in `staging` and `production`; production must not silently create schema.

Production database boot sequence:

1. Create the Postgres database.
2. Use a pgvector-capable Postgres image or managed service.
3. Set `DATABASE_URL`.
4. Run `alembic upgrade head`.
5. Start the API.
6. Verify `GET /health`.
7. Verify `GET /ready`.

`/health` is a lightweight liveness check. `/ready` verifies database connectivity, pgvector extension availability, required tables, and Alembic revision state.

Migration commands:

```bash
make migrate
make migration-check
make db-current
make db-history
make db-downgrade
```
