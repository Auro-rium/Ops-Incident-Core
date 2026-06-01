# Deployment Overview

IncidentOps Core uses one active deployment path: Azure Container Apps.

## Promotion Path

```text
CI tests
  -> Azure demo/staging
  -> Azure production
```

Local execution is only for development and CI tests. It is not a supported production/demo deployment path.

## Azure Stack

- Azure Container Registry
- Azure Container Apps:
  - Core API
  - Core worker
  - Core MCP server
  - Collector
  - frontend when frontend deployment is enabled
  - migration job
  - bootstrap admin job
- Azure Database for PostgreSQL Flexible Server with pgvector
- Azure Cache for Redis
- Azure Key Vault
- Azure Monitor / Log Analytics
- Azure OpenAI / Foundry deployments for chat and embeddings

## Deployment Order

1. Build/push images to ACR.
2. Deploy `infra/azure/main.bicep`.
3. Run migrations.
4. Bootstrap admin.
5. Configure Collector and MCP tokens.
6. Run Azure smoke.

Current workflow reality:

- `.github/workflows/deploy-azure.yml` runs on push to `core` and manual dispatch.
- It builds/pushes Core and Collector images by default.
- Frontend deployment support exists in the scripts and Bicep stack, but the current Core workflow uses `BUILD_FRONTEND=false` and `DEPLOY_FRONTEND=false`.

## Required Secrets

- `DATABASE_URL` generated into Key Vault
- `REDIS_PASSWORD`
- `JWT_SECRET`
- `BOOTSTRAP_ADMIN_EMAIL`
- `BOOTSTRAP_ADMIN_PASSWORD`
- `INCIDENTOPS_TOKEN` for Collector
- `INCIDENTOPS_MCP_TOKEN` for MCP
- `AZURE_OPENAI_API_KEY`

## Cost Control

Use [Azure cost guardrails](./azure-cost-guardrails.md). Delete the resource group when the demo is not needed:

```bash
CONFIRM=delete-$AZURE_RESOURCE_GROUP scripts/azure_teardown.sh
```
