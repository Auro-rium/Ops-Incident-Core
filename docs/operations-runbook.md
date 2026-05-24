# Operations Runbook

This runbook covers IncidentOps Core on Azure Container Apps.

## Health Checks

```bash
curl https://CORE_API_URL/health
curl https://CORE_API_URL/ready
curl https://CORE_API_URL/v1/capabilities
```

Expected readiness:

- database reachable
- pgvector extension available
- required tables and columns present
- Alembic revision at head

## Azure Checks

```bash
scripts/azure_login_check.sh
scripts/azure_smoke.sh
```

Container App status:

```bash
az containerapp list \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --query "[].{name:name,provisioningState:properties.provisioningState,runningStatus:properties.runningStatus}" \
  --output table
```

Logs:

```bash
az containerapp logs show --resource-group "$AZURE_RESOURCE_GROUP" --name incidentops-core-api --follow
az containerapp logs show --resource-group "$AZURE_RESOURCE_GROUP" --name incidentops-core-worker --follow
az containerapp logs show --resource-group "$AZURE_RESOURCE_GROUP" --name incidentops-mcp --follow
az containerapp logs show --resource-group "$AZURE_RESOURCE_GROUP" --name incidentops-collector --follow
az containerapp logs show --resource-group "$AZURE_RESOURCE_GROUP" --name incidentops-frontend --follow
```

## Common Failures

### `/ready` fails

Run migrations:

```bash
scripts/azure_run_migrations.sh
```

Then inspect API logs.

### API startup fails with unsafe production configuration

Check that production does not use local/demo settings:

```text
DB_CREATE_ALL=false
LOCAL_INGEST_ENABLED=false
ALLOW_LOCAL_SEED_ADMIN=false
ALLOW_DEMO_PROJECT_BYPASS=false
DEMO_MODE_PUBLIC=false
WORKER_MODE=queue
JOB_QUEUE_BACKEND=redis
RATE_LIMIT_BACKEND=redis
METRICS_BACKEND=prometheus
ALLOW_WILDCARD_CORS=false
```

### Azure OpenAI / Foundry errors

Verify:

- endpoint has no trailing deployment path
- API key is stored in Key Vault
- chat deployment name is correct
- embedding deployment name is correct
- embedding deployment supports `dimensions=384`
- `EMBEDDING_MODEL=azure-openai`

### Collector sync does not complete

Verify Collector has:

- Core API URL
- Core access token
- project ID
- allowed source path
- source registration permission

Then inspect Core source sync diagnostics.

### MCP tools fail

Verify MCP Container App has:

```text
MCP_TRANSPORT=streamable-http
MCP_CORE_API_URL=https://CORE_API_URL
MCP_TOKEN=<valid Core token>
```

The MCP server calls Core APIs and therefore fails if the token is expired, lacks project membership, or Core is not ready.

## Release Checklist

- Core tests pass
- Collector tests pass
- Frontend build passes
- images pushed to ACR
- Bicep deployment succeeds
- migrations pass
- admin bootstrap succeeds
- `/health` passes
- `/ready` passes
- capabilities endpoint works
- Collector sync succeeds
- search returns evidence
- investigate returns cited response or honest insufficient-evidence response
- readiness endpoint works
- workflow run reaches a valid state
- MCP server starts and can call Core with a valid token

## Teardown

Azure resources can keep billing after the demo. To delete the resource group:

```bash
CONFIRM=delete-$AZURE_RESOURCE_GROUP scripts/azure_teardown.sh
```
