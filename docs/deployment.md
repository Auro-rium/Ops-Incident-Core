# Deployment

## Production Database Boot Sequence

1. Create a Postgres database.
2. Ensure the database supports pgvector.
3. Set `DATABASE_URL`.
4. Run migrations:

```bash
alembic upgrade head
```

5. Start the API.
6. Verify liveness:

```bash
curl http://127.0.0.1:8001/health
```

7. Verify readiness:

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
