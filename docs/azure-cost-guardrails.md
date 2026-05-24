# Azure Cost Guardrails

IncidentOps Azure v2 is designed for a $200-credit demo, not an always-on production fleet.

## Budget Alerts

Create a Cost Management budget for the resource group with these alerts:

| Threshold | Type | Action |
|---|---|---|
| $25 | Actual | Check that the demo is still needed. |
| $75 | Actual | Stop nonessential Container Apps and review PostgreSQL/Redis spend. |
| $125 | Forecast | Decide whether to tear down before the credit is consumed. |
| $180 | Actual or forecast | Panic threshold: export data and tear down. |

Example:

```bash
az consumption budget create \
  --budget-name incidentops-demo-budget \
  --amount 200 \
  --category cost \
  --time-grain monthly \
  --resource-group incidentops-demo-rg \
  --start-date 2026-05-01 \
  --end-date 2026-12-31
```

The Azure CLI budget command surface changes across versions. If this command fails, create the budget in Azure Portal: Cost Management → Budgets → Add.

## Services That Keep Billing

These resources can continue billing even when nobody is using the demo:

- Azure Database for PostgreSQL Flexible Server
- Azure Cache for Redis
- Container Apps with `minReplicas > 0`
- Log Analytics ingestion and retention
- Container Registry storage
- Key Vault transactions and stored secrets

## Daily Cost Discipline

For a public demo, keep only one environment alive. Use small SKUs:

- PostgreSQL Flexible Server: burstable B1ms, 32GB storage
- Redis: Basic C0
- Container Apps: one replica for API, worker, frontend; Collector can scale to zero unless actively syncing
- Log Analytics: 30-day retention

## Teardown

Teardown removes the entire resource group:

```bash
CONFIRM=delete-incidentops-demo-rg \
AZURE_RESOURCE_GROUP=incidentops-demo-rg \
scripts/azure_teardown.sh
```

Before teardown, export anything needed from PostgreSQL. The demo stack does not promise durable backups unless you configure them explicitly.
