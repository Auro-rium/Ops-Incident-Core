# Operations Runbook

This runbook covers operating and debugging IncidentOps Core in local, EC2 demo, and production-style environments.

## Golden rule

Do not change product logic during an incident or demo failure unless the system is genuinely broken and the fix is understood. Most demo failures are configuration, secrets, ports, containers, migrations, or stale volumes. Software loves making small mistakes look like philosophical problems.

## Basic health checks

Liveness:

```bash
curl http://HOST/health
```

Readiness:

```bash
curl http://HOST/ready
```

Capabilities:

```bash
curl http://HOST/api/v1/capabilities
```

Direct local API may use `/v1/capabilities`; EC2 demo through Nginx uses `/api/v1/capabilities`.

Expected readiness includes:

- database reachable
- pgvector extension available
- required tables and columns present
- Alembic revision at head

## EC2 demo checks

SSH:

```bash
ssh -i ~/.ssh/YOUR_KEY.pem ubuntu@EC2_PUBLIC_IP
```

Stack status:

```bash
cd ~/incidentops/Ops-Incident-Core
docker compose -f docker-compose.ec2-demo.yml ps
```

Smoke:

```bash
scripts/smoke_ec2_demo.sh
```

Logs:

```bash
docker compose -f docker-compose.ec2-demo.yml logs nginx --tail=100
docker compose -f docker-compose.ec2-demo.yml logs api --tail=150
docker compose -f docker-compose.ec2-demo.yml logs core-worker --tail=150
docker compose -f docker-compose.ec2-demo.yml logs collector --tail=150
docker compose -f docker-compose.ec2-demo.yml logs postgres --tail=100
docker compose -f docker-compose.ec2-demo.yml logs redis --tail=100
docker compose -f docker-compose.ec2-demo.yml logs frontend --tail=100
```

## Common failures

### Frontend loads wrong UI

Cause: EC2 stack built from Core `./apps/web` instead of separate `Ops-Incident-frontend`.

Fix:

```bash
scripts/deploy_ec2_demo.sh \
  --public-url http://EC2_PUBLIC_IP \
  --collector-repo ../Ops-Incident-Collector \
  --frontend-repo ../Ops-Incident-frontend
```

Check compose uses:

```text
${FRONTEND_REPO_PATH:-../Ops-Incident-frontend}
```

### `/ready` fails

Check migrations:

```bash
docker compose -f docker-compose.ec2-demo.yml run --rm migrate
```

Then:

```bash
docker compose -f docker-compose.ec2-demo.yml logs api --tail=150
```

If readiness reports missing columns, the database schema is drifted. For disposable demo DBs, recreate the volume only after backing up. For production, use migrations or restore from known-good backup. No, dropping production DBs is not a migration strategy.

### Collector cannot reach Core

Check Collector health:

```bash
curl http://HOST/collector/health
```

Inside EC2:

```bash
docker compose -f docker-compose.ec2-demo.yml logs collector --tail=150
```

Verify env files:

- `deploy/ec2/.env.demo`
- `deploy/ec2/.env.runtime`

Verify Collector uses internal Core URL:

```text
INCIDENTOPS_API_URL=http://api:8000
```

### Search returns zero results

Check sync status:

```bash
scripts/smoke_ec2_demo.sh
```

Inspect Collector logs and Core source sync diagnostics. Common causes:

- Collector did not sync
- token missing/invalid
- source registration failed
- batch ingest failed
- documents were skipped due to policy
- wrong project ID

### Investigation says evidence is insufficient

This may be correct. The system should not invent root causes. Check evidence count, citations, missing data, and fixture quality before blaming the investigation code like a ritual sacrifice.

### Workflow stuck awaiting approval

This can be expected if risky action drafts are approval-gated. Check run events:

```http
GET /v1/runs/{run_id}/events
```

Through Nginx:

```http
GET /api/v1/runs/{run_id}/events
```

## Backup and teardown

Backup DB:

```bash
scripts/backup_db.sh
```

Stop stack:

```bash
scripts/teardown_ec2_demo.sh
```

Stop EC2:

```bash
aws ec2 stop-instances --instance-ids INSTANCE_ID --region us-east-1
```

Terminate EC2 when done:

```bash
aws ec2 terminate-instances --instance-ids INSTANCE_ID --region us-east-1
```

## Security checks

For EC2 demo, confirm only public ports are exposed:

- `22/tcp` from operator IP only
- `80/tcp` public
- `443/tcp` public when configured

Do not expose:

- `5432` Postgres
- `6379` Redis
- `8000/8001` Core API direct
- `8686` Collector direct
- `3000` frontend direct

Generated secrets live in EC2 env files and must not be committed.

## Release checklist

Before declaring a release/demo ready:

- tests pass
- Docker build passes
- migrations pass
- `/health` passes
- `/ready` passes
- capabilities endpoint works
- Collector health is healthy
- Collector core reachable is true
- sync succeeds
- search returns evidence
- investigate returns cited response or honest insufficient-evidence response
- workflow run reaches valid state
- frontend is from `Ops-Incident-frontend`
- internal ports are closed externally
- DB backup command works

## Demo script

1. Open public frontend URL.
2. Show Core status/readiness.
3. Show Collector health and sync status.
4. Ask an incident question.
5. Show evidence and citations.
6. Show investigation result, confidence, missing data.
7. Show workflow run events/approval state.
8. Mention Collector/Core/frontend split.
9. Mention that weak evidence is handled honestly.

That is the product story. Not a chatbot. Not a screenshot factory. An evidence-backed incident investigation system.
