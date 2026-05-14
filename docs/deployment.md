# Deployment

## Production Database Boot Sequence

1. Create a Postgres database.
2. Ensure the database supports pgvector.
3. Set `DATABASE_URL`.
4. Run migrations:

```bash
alembic upgrade head
```

5. Bootstrap an admin user:

```bash
python -m incidentops.security.bootstrap_admin \
  --email admin@example.com \
  --password "use-a-strong-password"
```

6. Start the API.
7. Verify liveness:

```bash
curl http://127.0.0.1:8001/health
```

8. Verify readiness:

```bash
curl http://127.0.0.1:8001/ready
```

## Environment Modes

Supported modes:

```text
APP_ENV=local
APP_ENV=development
APP_ENV=staging
APP_ENV=production
```

`DB_CREATE_ALL=false` is the default. `DB_CREATE_ALL=true` is only honored in `local` or `development` and exists for developer convenience. It is ignored in `staging` and `production`.

Production rule: run Alembic migrations before starting the app. The app must not create schema with SQLAlchemy `create_all` in production.

## Production Security Settings

Set these explicitly for staging/production:

```text
JWT_SECRET=<strong-random-secret-at-least-32-chars>
JWT_ALGORITHM=HS256
JWT_ISSUER=incidentops
JWT_AUDIENCE=incidentops-api
ACCESS_TOKEN_EXPIRE_MINUTES=60
ALLOW_LOCAL_SEED_ADMIN=false
ALLOW_DEMO_PROJECT_BYPASS=false
DEMO_MODE_PUBLIC=false
RATE_LIMIT_BACKEND=redis
CORS_ALLOW_ORIGINS=https://your-ui.example.com
ALLOW_WILDCARD_CORS=false
```

Startup fails in staging/production if default JWT secrets, local seed admin, demo bypass, wildcard CORS, `DB_CREATE_ALL=true`, or in-memory rate limiting are configured.

Passwords are stored with bcrypt. Legacy SHA256 hashes are only accepted in local/development and are rehashed after a successful login.

Source config must not contain raw credentials. Put secret material in a secret manager and pass only a `credentials_ref`.

## Collector Ingestion Limits

Collector batch ingest is bounded by:

```text
MAX_DOCUMENTS_PER_BATCH=100
MAX_DOCUMENT_BYTES=2000000
MAX_BATCH_BYTES=10000000
MAX_CHUNKS_PER_DOCUMENT=500
MAX_METADATA_BYTES=64000
MAX_EXTERNAL_ID_LENGTH=1024
MAX_PATH_LENGTH=2048
```

If a whole batch exceeds count or byte limits, the API rejects the request. If one document is invalid or oversized, the batch response includes a per-document error and continues indexing valid documents. Error responses do not include raw document content.

Production ingestion should use the source/collector/sync/document-batch APIs. `/v1/projects/{project_id}/ingest` remains available for local development and server-visible folder smoke tests only.

## RBAC

Roles are project-scoped:

- `viewer`: read evidence/search results, runs, reports, and eval results.
- `investigator`: create investigations, answers, workflow runs, and local/dev ingests.
- `approver`: approve or reject gated workflow actions.
- `admin`: manage sources, collectors, syncs, document batch ingest, project settings, members, and eval runs.

Project membership is enforced for source/sync/search/answer/investigate/runs/evals/approvals. Local demo project bypass is available only outside staging/production when explicitly enabled.

## Audit Events

The `audit_events` table records login success/failure, project creation, source and collector changes, sync lifecycle, document batch ingest, investigations, workflow runs, approval decisions, eval runs, permission denials, and rate-limit blocks. Audit metadata is redacted before storage.

## Checks

`/health` is a lightweight liveness check and returns process health.

`/ready` checks:

- database connectivity
- pgvector extension
- required application tables
- Alembic current revision against head revision

Run the same readiness check from the CLI:

```bash
python scripts/check_migrations.py
```

## Make Commands

```bash
make migrate
make migration-check
make db-current
make db-history
make db-downgrade
```
