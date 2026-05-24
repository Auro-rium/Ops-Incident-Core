# IncidentOps Azure Infrastructure

This folder contains the Azure v2 demo deployment for IncidentOps Core.

It intentionally uses a small Azure-native stack:

- Azure Container Registry
- Azure Container Apps for Core API, Core worker, MCP server, Collector, and frontend
- Azure Database for PostgreSQL Flexible Server
- Azure Cache for Redis
- Azure Key Vault
- Log Analytics

It intentionally does not use AKS, NAT Gateway, or multi-region networking. Azure OpenAI / Foundry is required for staging/production.

## Deploy Order

From the Core repo:

```bash
export AZURE_RESOURCE_GROUP=incidentops-demo-swc-rg
export AZURE_LOCATION=swedencentral
export ACR_NAME=<globally-unique-acr-name>  # live demo uses incidentopsacr6763
export CORS_ORIGINS=https://<frontend-container-app-fqdn>
export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
export AZURE_OPENAI_API_KEY=<secret>
export AZURE_OPENAI_CHAT_DEPLOYMENT=<chat-deployment-name>
export AZURE_OPENAI_EMBEDDING_DEPLOYMENT=<embedding-deployment-name>

scripts/azure_build_push_images.sh
scripts/azure_deploy.sh
scripts/azure_run_migrations.sh
scripts/azure_bootstrap_admin.sh
scripts/azure_smoke.sh
```

`scripts/azure_build_push_images.sh` creates the resource group and ACR if needed so images can exist before Container Apps are deployed.

## Required Secret Inputs

Pass these as environment variables or GitHub Actions secrets:

- `POSTGRES_PASSWORD`
- `JWT_SECRET`
- `BOOTSTRAP_ADMIN_EMAIL`
- `BOOTSTRAP_ADMIN_PASSWORD`
- `AZURE_OPENAI_API_KEY`

If omitted locally, `scripts/azure_deploy.sh` generates strong values for the first deploy. Keep those values somewhere safe; they are not committed.

## Optional Collector Inputs

The collector Container App starts at zero replicas unless both are supplied:

- `INCIDENTOPS_TOKEN`
- `INCIDENTOPS_PROJECT_ID`

The smoke script can create a project, update the private Collector app with a short-lived token, and verify sync without exposing Collector publicly.

## MCP Server

The deployment includes a private MCP Container App. It is configured by setting:

- `INCIDENTOPS_MCP_TOKEN`

The smoke script can update this token from a fresh Core login. Keep the MCP app private unless an authentication gateway is added.

## Azure OpenAI / Foundry

Core uses Azure chat completions and embeddings endpoints with deployment names:

- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_API_VERSION`
- `AZURE_OPENAI_CHAT_DEPLOYMENT`
- `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`

Production uses `EMBEDDING_MODEL=azure-openai` and expects the embedding deployment to support `dimensions=384`.
