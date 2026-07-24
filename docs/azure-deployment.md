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
  - `incidentops-frontend` when frontend deployment is enabled
- Azure Database for PostgreSQL Flexible Server
- Azure Cache for Redis
- Azure Key Vault
- Log Analytics / Azure Monitor
- Azure ML managed GPU endpoints when RAG v2 is enabled

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
- Docker for local image builds and pushes to ACR
- GitHub CLI only if you are configuring CI/CD manually

Login:

```bash
az login
az account set --subscription <subscription-id>
```

Set deployment variables:

```bash
export AZURE_RESOURCE_GROUP=incidentops-demo-swc-rg
export AZURE_LOCATION=swedencentral
export ACR_NAME=<globally-unique-acr-name>  # live demo uses incidentopsacr6763
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

This creates the resource group and ACR if needed, then uses local Docker builds and `docker push` to publish:

- `incidentops-core:<tag>`
- `opsincident-collector:<tag>`
- `incidentops-frontend:<tag>` only when `BUILD_FRONTEND=true`

Current GitHub workflow reality:

- `.github/workflows/deploy-azure.yml` builds and deploys Core plus Collector on pushes to `core`.
- That workflow checks out the Collector repo and runs `scripts/azure_build_push_images.sh` with `BUILD_FRONTEND=false`.
- The same workflow runs `scripts/azure_deploy.sh` with `DEPLOY_FRONTEND=false`, so frontend infrastructure can remain deployed in Azure without being rebuilt on every Core push.
- Frontend deployment is therefore supported by the infrastructure and scripts, but not enabled by default in the Core workflow.

## Deploy Infrastructure

```bash
scripts/azure_deploy.sh
```

The deployment creates Container Apps, PostgreSQL, Redis, Key Vault, and Log Analytics. If `DEPLOY_FRONTEND=false`, the Bicep deployment leaves frontend deployment disabled while still updating the Core-side runtime.

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

RAG v2 does not run models in Core, Collector, or local development. It uses two Azure ML managed GPU scoring endpoints, one for BGE-M3 embeddings and one for the BGE cross-encoder reranker. See [Cloud-only GPU RAG runtime](./rag-v2-cloud-runtime.md).

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

The v1 Azure OpenAI embedding deployment must support `dimensions=384` because the legacy pgvector schema stores `Vector(384)`. The optional GPU RAG v2 path stores BGE-M3 vectors in a separate `Vector(1024)` table.

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

GitHub workflow:

```text
.github/workflows/deploy-azure.yml
```

Trigger behavior:

- push to `core`
- manual `workflow_dispatch`

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

- `AZURE_RESOURCE_GROUP` (live demo: `incidentops-demo-swc-rg`)
- `AZURE_LOCATION` (live demo: `swedencentral`)
- `ACR_NAME` (live demo: `incidentopsacr6763`)
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

Latest verified Core Azure runs in GitHub at the time of this doc update:

- `26735683633` — success — deployed collector batch upload rate-limit separation
- `26735351384` — success — deployed embedding work off the API event loop
- `26734478346` — success — deployed embedding retry/backoff and bounded fallback chunking

## Teardown

```bash
CONFIRM=delete-incidentops-demo-swc-rg \
AZURE_RESOURCE_GROUP=incidentops-demo-swc-rg \
scripts/azure_teardown.sh
```

This deletes PostgreSQL, Redis, Container Apps, images, Key Vault, and logs in that resource group. Review [Azure cost guardrails](azure-cost-guardrails.md) before leaving the demo running.

## Caveats

This is not yet a hardened enterprise network deployment. PostgreSQL and Redis are provisioned in the smallest practical demo shape, and large-repo benchmark quality is still being hardened. Before calling it production-grade, move stateful services behind private networking, add backup/restore drills, define retention, and publish repeatable large-repo benchmark results after the current ingestion fixes.
