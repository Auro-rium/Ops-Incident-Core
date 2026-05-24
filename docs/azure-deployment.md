# Azure v2 Deployment

This is the Azure-native deployment path for IncidentOps. It keeps the architecture small enough for a $200 credit while proving the product loop:

Frontend → Core API → Collector → PostgreSQL/pgvector → Redis/worker → search → investigate → workflow → MCP tools.

## Architecture

Azure services used:

- Azure Container Registry for images
- Azure Container Apps for:
  - `incidentops-core-api`
  - `incidentops-core-worker`
  - `incidentops-mcp`
  - `incidentops-collector`
  - `incidentops-frontend`
- Azure Database for PostgreSQL Flexible Server
- Azure Cache for Redis
- Azure Key Vault
- Log Analytics / Azure Monitor

Azure services intentionally not used:

- AKS
- NAT Gateway
- multi-region networking
- API Management
- Application Gateway
- Azure OpenAI / Foundry is required for staging/production and must be supplied through Key Vault-backed settings.

## Prerequisites

Install:

- Azure CLI
- Docker if you want local image builds, though the scripts use ACR remote builds
- GitHub CLI only if you are configuring CI/CD manually

Login:

```bash
az login
az account set --subscription <subscription-id>
```

Set deployment variables:

```bash
export AZURE_RESOURCE_GROUP=incidentops-demo-rg
export AZURE_LOCATION=eastus
export ACR_NAME=<globally-unique-acr-name>
export NAME_PREFIX=incidentops
export ENVIRONMENT_NAME=demo
export IMAGE_TAG=$(git rev-parse --short HEAD)

export POSTGRES_PASSWORD="$(openssl rand -hex 24 | tr -d '\n')"
export JWT_SECRET="$(openssl rand -base64 48 | tr -d '\n')"
export BOOTSTRAP_ADMIN_EMAIL=admin@example.com
export BOOTSTRAP_ADMIN_PASSWORD="$(openssl rand -hex 20 | tr -d '\n')"
export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
export AZURE_OPENAI_API_KEY=<secret>
export AZURE_OPENAI_API_VERSION=2024-10-21
export AZURE_OPENAI_CHAT_DEPLOYMENT=<chat-deployment-name>
export AZURE_OPENAI_EMBEDDING_DEPLOYMENT=<embedding-deployment-name>
```

Do not commit these values.

## Build and Push Images

The Core repo should sit beside Collector and frontend:

```text
~/incidentops/
  Ops-Incident-Core/
  Ops-Incident-Collector/        # or OpsIncident-Collector
  Ops-Incident-frontend/         # or incidentops-frontend
```

From Core:

```bash
scripts/azure_build_push_images.sh
```

This creates the resource group and ACR if needed, then pushes:

- `incidentops-core:<tag>`
- `opsincident-collector:<tag>`
- `incidentops-frontend:<tag>`

## Deploy Infrastructure

```bash
scripts/azure_deploy.sh
```

The deployment creates Container Apps, PostgreSQL, Redis, Key Vault, and Log Analytics.

Production settings enforced by the Container Apps:

```text
APP_ENV=production
DB_CREATE_ALL=false
DB_REQUIRE_MIGRATIONS=true
ENABLE_LOCAL_INGEST=false
WORKER_MODE=queue
JOB_QUEUE_BACKEND=redis
RATE_LIMIT_BACKEND=redis
METRICS_PUBLIC=false
ALLOW_DEMO_PROJECT_BYPASS=false
ALLOW_LOCAL_SEED_ADMIN=false
REQUIRE_AZURE_OPENAI=true
EMBEDDING_MODEL=azure-openai
```

## Migrations and Bootstrap

Run migrations as a Container Apps job:

```bash
scripts/azure_run_migrations.sh
```

Create the bootstrap admin:

```bash
scripts/azure_bootstrap_admin.sh
```

The API startup also runs `scripts/check_migrations.py` before serving traffic.

## Smoke Test

```bash
SMOKE_EMAIL="$BOOTSTRAP_ADMIN_EMAIL" \
SMOKE_PASSWORD="$BOOTSTRAP_ADMIN_PASSWORD" \
scripts/azure_smoke.sh
```

The smoke verifies:

- frontend URL loads
- Core `/health`
- Core `/ready`
- `/v1/capabilities`
- login and project creation
- private Collector app can sync fixture data into Core
- search returns evidence
- investigate returns a response
- readiness endpoint returns a score
- private MCP app is configured with a Core token

Collector is not exposed publicly. The smoke updates its private Container App with a short-lived Core token and project id.

## Azure OpenAI / Foundry

Azure OpenAI / Foundry is required for deployed staging/production. Azure uses deployment names, not plain model names:

```bash
export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
export AZURE_OPENAI_API_KEY=<secret>
export AZURE_OPENAI_API_VERSION=2024-10-21
export AZURE_OPENAI_CHAT_DEPLOYMENT=<chat-deployment-name>
export AZURE_OPENAI_EMBEDDING_DEPLOYMENT=<embedding-deployment-name>
```

The embedding deployment must support `dimensions=384` because the current pgvector schema stores `Vector(384)`.

## MCP Server

The deployment includes a separate private Container App:

```text
incidentops-mcp
```

It runs:

```bash
python -m incidentops.mcp.server
```

with:

```text
MCP_TRANSPORT=streamable-http
MCP_HOST=0.0.0.0
MCP_PORT=8080
MCP_PATH=/mcp
MCP_CORE_API_URL=https://<core-api-fqdn>
MCP_TOKEN=<Core access token>
```

The MCP app is private by default. Do not make it public without adding an explicit authentication gateway.

## GitHub Actions

Manual workflow:

```text
.github/workflows/deploy-azure.yml
```

Required GitHub secrets:

- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_SUBSCRIPTION_ID`
- `POSTGRES_PASSWORD`
- `JWT_SECRET`
- `BOOTSTRAP_ADMIN_EMAIL`
- `BOOTSTRAP_ADMIN_PASSWORD`
- `AZURE_OPENAI_API_KEY`

Use URL-safe PostgreSQL passwords. The helper script generates hex passwords so the SQLAlchemy `DATABASE_URL` remains valid.

Required GitHub variables:

- `AZURE_RESOURCE_GROUP`
- `AZURE_LOCATION`
- `ACR_NAME`
- `CORS_ORIGINS`
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_VERSION`
- `AZURE_OPENAI_CHAT_DEPLOYMENT`
- `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`

Optional:

- `GH_READ_TOKEN` if Collector/frontend repos are private
- `INCIDENTOPS_TOKEN`
- `INCIDENTOPS_PROJECT_ID`
- `INCIDENTOPS_MCP_TOKEN`

## Teardown

```bash
CONFIRM=delete-incidentops-demo-rg \
AZURE_RESOURCE_GROUP=incidentops-demo-rg \
scripts/azure_teardown.sh
```

This deletes PostgreSQL, Redis, Container Apps, images, Key Vault, and logs in that resource group. Review [Azure cost guardrails](azure-cost-guardrails.md) before leaving the demo running.

## Caveats

This is not yet a hardened enterprise network deployment. For budget and complexity reasons, PostgreSQL and Redis are provisioned in the smallest practical demo shape. Before calling it production-grade, move stateful services behind private networking, add backup/restore drills, define retention, and run real customer-data validation.
