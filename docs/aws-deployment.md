# AWS Deployment

This guide deploys IncidentOps Core on AWS with ECS Fargate, RDS PostgreSQL, ElastiCache Redis, Secrets Manager, ECR, an Application Load Balancer, CloudWatch logs, and GitHub Actions CI/CD.

The Core production path is:

```text
ALB -> ECS API service
API/worker -> RDS PostgreSQL with pgvector extension created by Alembic
API/worker -> ElastiCache Redis for queue/rate-limit/runtime state
GitHub Actions -> ECR -> one-off migration task -> ECS API + worker services -> smoke_prod.py
```

No production startup path runs SQLAlchemy `create_all`. Migrations must run before services are updated.

## Prerequisites

- Terraform `>= 1.6`
- AWS account with permissions for VPC, ECS, ECR, RDS, ElastiCache, Secrets Manager, IAM, ALB, and CloudWatch
- GitHub OIDC role allowed to deploy this stack
- A pgvector-capable RDS PostgreSQL engine version
- Optional ACM certificate ARN for HTTPS on the ALB

## Terraform Bootstrap

Create a Terraform variables file from the example:

```bash
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
```

Edit:

```hcl
aws_region               = "us-east-1"
cors_origins             = "https://your-frontend.example.com"
bootstrap_admin_email    = "admin@example.com"
bootstrap_admin_password = "use-a-strong-password"
certificate_arn          = "arn:aws:acm:..."
```

Initialize and apply:

```bash
terraform -chdir=infra/terraform init
terraform -chdir=infra/terraform fmt -recursive
terraform -chdir=infra/terraform validate
terraform -chdir=infra/terraform apply
```

Important outputs:

```bash
terraform -chdir=infra/terraform output api_url
terraform -chdir=infra/terraform output ecr_repository_name
terraform -chdir=infra/terraform output ecr_repository_url
terraform -chdir=infra/terraform output ecs_cluster_name
terraform -chdir=infra/terraform output ecs_api_service_name
terraform -chdir=infra/terraform output ecs_worker_service_name
terraform -chdir=infra/terraform output ecs_api_task_definition_family
terraform -chdir=infra/terraform output ecs_worker_task_definition_family
```

## AWS Resources

Terraform provisions:

- VPC with public and private subnets
- Internet gateway and NAT gateway
- ECR repository for the Core image
- ECS cluster with container insights
- ECS Fargate service `incidentops-core-api`
- ECS Fargate service `incidentops-core-worker`
- Application Load Balancer and target group
- RDS PostgreSQL in private subnets
- ElastiCache Redis in private subnets
- Secrets Manager secrets for:
  - `DATABASE_URL`
  - `REDIS_URL`
  - `JWT_SECRET`
  - `BOOTSTRAP_ADMIN_EMAIL`
  - `BOOTSTRAP_ADMIN_PASSWORD`
  - `OPENAI_API_KEY` metadata, with value only when provided
  - `CORS_ORIGINS`
- CloudWatch log groups for API and worker
- IAM task execution and task roles
- Security groups:
  - ALB to API
  - API/worker to RDS
  - API/worker to Redis

## Production Environment

Production ECS tasks use these safety settings:

```text
APP_ENV=production
DB_CREATE_ALL=false
DB_REQUIRE_MIGRATIONS=true
WORKER_MODE=queue
JOB_QUEUE_BACKEND=redis
RATE_LIMIT_BACKEND=redis
METRICS_BACKEND=prometheus
METRICS_PUBLIC=false
LOCAL_INGEST_ENABLED=false
ENABLE_LOCAL_INGEST=false
ALLOW_LOCAL_SEED_ADMIN=false
ALLOW_DEMO_PROJECT_BYPASS=false
DEMO_MODE_PUBLIC=false
ALLOW_WILDCARD_CORS=false
```

`CORS_ORIGINS` and `CORS_ALLOW_ORIGINS` are both supported; the ECS task injects the same Secrets Manager value into both names.

Use `.env.production.example` as a readable inventory only. Real values belong in Secrets Manager and GitHub Actions secrets or variables.

## Migrations

The deployment workflow runs migrations as a one-off ECS Fargate task before updating services:

```bash
alembic upgrade head
python scripts/check_migrations.py
```

If either command fails, deployment stops. API and worker services are not updated.

Manual migration task pattern:

```bash
aws ecs run-task \
  --cluster "$ECS_CLUSTER" \
  --launch-type FARGATE \
  --task-definition "$ECS_API_TASK_DEFINITION" \
  --network-configuration "$NETWORK_CONFIGURATION" \
  --overrides '{"containerOverrides":[{"name":"api","command":["sh","-c","alembic upgrade head && python scripts/check_migrations.py"]}]}'
```

`/ready` verifies database connectivity, pgvector, required tables and columns, and Alembic revision after deployment.

## Bootstrap Admin

After the first migration, create or update the first admin with a one-off ECS task:

```bash
python -m incidentops.security.bootstrap_admin \
  --email "$BOOTSTRAP_ADMIN_EMAIL" \
  --password "$BOOTSTRAP_ADMIN_PASSWORD" \
  --update-password
```

Do not enable local seed admin in production.

## GitHub Actions CI/CD

Workflow:

```text
.github/workflows/deploy-core.yml
```

Triggers:

- push to `main`
- push to `core`
- push to `deploy`
- manual `workflow_dispatch`

Jobs:

1. `lint`: `uv run --extra dev ruff check .`
2. `tests`: starts local pgvector Postgres and Redis, migrates, starts the API, then runs unit and integration tests
3. `build-and-push`: builds Docker image and pushes to ECR
4. `deploy`: registers new API and worker task revisions, runs migration one-off task, deploys both services, waits for stability, then runs `smoke_prod.py`

Required GitHub secrets:

```text
AWS_ROLE_TO_ASSUME
SMOKE_EMAIL
SMOKE_PASSWORD
```

Required GitHub repository variables or secrets:

```text
AWS_REGION
ECR_REPOSITORY
ECS_CLUSTER
ECS_API_SERVICE
ECS_WORKER_SERVICE
ECS_API_TASK_DEFINITION
ECS_WORKER_TASK_DEFINITION
API_BASE_URL
```

Optional variables:

```text
ECS_API_CONTAINER_NAME=api
ECS_WORKER_CONTAINER_NAME=worker
```

Use the Terraform outputs to populate the ECS/ECR values.

## Smoke Test

CI runs:

```bash
uv run --extra dev python scripts/smoke_prod.py \
  --base-url "$API_BASE_URL" \
  --email "$SMOKE_EMAIL" \
  --password "$SMOKE_PASSWORD" \
  --query "What does this tiny service evidence say?"
```

The default smoke test uses normalized document batch ingest and does not require server-visible local files. Its eval sub-check is best effort because custom eval case files may not be visible to the remote API container; eval failure is reported as a warning and does not fail the smoke when the API/search/investigate/workflow checks pass.

## Rollback

Fast rollback:

```bash
aws ecs update-service \
  --cluster "$ECS_CLUSTER" \
  --service "$ECS_API_SERVICE" \
  --task-definition "$PREVIOUS_API_TASK_DEFINITION"

aws ecs update-service \
  --cluster "$ECS_CLUSTER" \
  --service "$ECS_WORKER_SERVICE" \
  --task-definition "$PREVIOUS_WORKER_TASK_DEFINITION"
```

Then wait for stability:

```bash
aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_API_SERVICE"
aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_WORKER_SERVICE"
```

Do not roll back a migration after live writes unless a tested downgrade path exists. Prefer forward-compatible migrations and task-definition rollback first.

## Local vs Production

Local development may use:

```text
WORKER_MODE=inline
JOB_QUEUE_BACKEND=inline
RATE_LIMIT_BACKEND=memory
LOCAL_INGEST_ENABLED=true
ALLOW_LOCAL_SEED_ADMIN=true
```

Production must use:

```text
WORKER_MODE=queue
JOB_QUEUE_BACKEND=redis
RATE_LIMIT_BACKEND=redis
LOCAL_INGEST_ENABLED=false
ALLOW_LOCAL_SEED_ADMIN=false
```

The production app refuses unsafe settings at startup.
