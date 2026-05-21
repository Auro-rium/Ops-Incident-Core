# Deployment Overview

IncidentOps Core supports two deployment paths:

1. A budget-safe EC2 Docker Compose demo stack.
2. A more production-shaped managed AWS stack with ECS/RDS/Redis/ALB.

Do not confuse those. One is for proving the product publicly without torching credits. The other is for long-running production operations, where AWS bills you with the warmth of a parking meter.

## Recommended promotion path

```text
local dev
  -> local Docker/E2E
  -> budget-safe EC2 demo
  -> managed AWS staging
  -> managed AWS production
```

## EC2 demo stack

The EC2 demo stack is intended for portfolio/demo validation. It runs on a single EC2 instance using Docker Compose.

Services:

- Nginx public on `80/443`
- frontend from `Ops-Incident-frontend`
- Core API
- Core worker
- Postgres with pgvector
- Redis
- Collector daemon from `Ops-Incident-Collector`
- migration and admin-bootstrap tool services

Public surface:

- `80/tcp` public
- `443/tcp` public when TLS is configured
- `22/tcp` restricted to the operator IP

Internal only:

- Postgres `5432`
- Redis `6379`
- Core API `8000/8001`
- Collector health `8686`
- frontend internal `3000`

Deploy:

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

Stop when done:

```bash
scripts/backup_db.sh
scripts/teardown_ec2_demo.sh
aws ec2 stop-instances --instance-ids INSTANCE_ID --region us-east-1
```

Terminate when the demo is no longer needed:

```bash
aws ec2 terminate-instances --instance-ids INSTANCE_ID --region us-east-1
```

## Managed AWS stack

The managed stack is intended for a longer-running deployment.

Core resources:

- ECR repository
- ECS Fargate API service
- ECS Fargate worker service
- RDS PostgreSQL
- pgvector through migrations
- ElastiCache Redis/Valkey
- Secrets Manager
- CloudWatch logs
- Application Load Balancer
- ECS one-off migration task
- GitHub Actions CI/CD

Production settings:

```text
APP_ENV=production
DB_CREATE_ALL=false
DB_REQUIRE_MIGRATIONS=true
WORKER_MODE=queue
JOB_QUEUE_BACKEND=redis
RATE_LIMIT_BACKEND=redis
ENABLE_LOCAL_INGEST=false
METRICS_PUBLIC=false
ALLOW_DEMO_PROJECT_BYPASS=false
ALLOW_LOCAL_SEED_ADMIN=false
```

## Deployment order

For managed deployment:

1. Provision database, Redis, ECR, ECS, IAM, logs, and ALB.
2. Build and push Core image.
3. Run Alembic migration one-off task.
4. Run `scripts/check_migrations.py`.
5. Deploy Core API.
6. Deploy Core worker.
7. Bootstrap admin.
8. Run `scripts/smoke_prod.py`.
9. Deploy Collector.
10. Deploy frontend.
11. Run full browser and system smoke.

## Required secrets

Do not commit secrets. Use `.env` locally, generated EC2 env files for the demo stack, or Secrets Manager in AWS.

Important secrets:

- `DATABASE_URL`
- `REDIS_URL`
- `JWT_SECRET`
- `BOOTSTRAP_ADMIN_EMAIL`
- `BOOTSTRAP_ADMIN_PASSWORD`
- `INCIDENTOPS_TOKEN` for Collector
- optional LLM provider keys

## GitHub Actions

Core includes workflow assets for deployment. The EC2 demo redeploy workflow should be manual-only and SSH into the existing demo host. The managed AWS workflow should use OIDC, build images, push to ECR, run migration, deploy API/worker, wait for service stability, and run smoke.

Avoid long-lived AWS keys when OIDC is available. Apparently cloud security prefers not to be held together with a text file named `keys-final-final.txt`.

## Rollback basics

For EC2 demo:

- keep previous Git commits available
- rerun deploy from a known-good commit
- restore DB from `scripts/backup_db.sh` output if needed

For ECS:

- roll back ECS task definition revision
- restore RDS snapshot if schema/data rollback is required
- rerun smoke after rollback

## Cost control

For student/demo usage:

- prefer EC2 demo stack first
- stop EC2 when not demoing
- avoid NAT Gateway unless necessary
- avoid leaving RDS/ElastiCache/ALB running casually
- configure AWS Budgets alerts

Budget discipline is not optional. AWS will not notice your ambition and apply a kindness discount.
