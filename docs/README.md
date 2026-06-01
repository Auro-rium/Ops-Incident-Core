# Documentation Index

This folder contains the current documentation for IncidentOps Core.

The active product story is Azure-first:

```text
Collector -> Core API -> PostgreSQL/pgvector -> Azure OpenAI -> readiness/search/investigate -> Core MCP -> frontend
```

Local execution is kept only for deterministic development and CI safety. It is not the flagship product proof path.

## Current Documentation Map

- [Architecture](./architecture.md) — runtime split, component responsibilities, retrieval/investigation flow, and current failure modes.
- [System architecture](./system-architecture.md) — repository boundaries, data model, ingestion design, and service-level system view.
- [Azure deployment](./azure-deployment.md) — Azure Container Apps, ACR, PostgreSQL, Redis, Key Vault, Log Analytics, Azure OpenAI, and CI/CD.
- [Deployment](./deployment.md) — production topology, runtime settings, rollout sequence, and rollback notes.
- [Deployment overview](./deployment-overview.md) — short-form Azure stack and promotion path.
- [Azure cost guardrails](./azure-cost-guardrails.md) — budget alerts, teardown, and cloud cost risks.
- [Cloud-only user E2E runbook](./user-e2e-runbook.md) — real Azure user proof with benchmark ingestion, runtime status, and MCP smoke.
- [Operations runbook](./operations-runbook.md) — rollout, smoke, MCP, and Azure troubleshooting checks.
- [Product proof](./product-proof.md) — what counts as product proof, what is actually proven, and what not to overclaim.
- [Temporal benchmark](./temporal-benchmark.md) — current large-repo benchmark status, live failure points, and rerun criteria.
- [MCP architecture](./mcp-architecture.md) — Core MCP as the only active MCP surface in the current codebase.
- [Collector/Core contract](./collector-core-contract.md) — versioned Collector-to-Core ingestion contract.
- [Security](./security.md) — threat model, trust boundaries, controls, and verification checklist.

## Documentation Cleanup Result

Kept:

- `architecture.md`
- `azure-deployment.md`
- `azure-cost-guardrails.md`
- `user-e2e-runbook.md`
- `product-proof.md`
- `temporal-benchmark.md`
- `mcp-architecture.md`
- `collector-core-contract.md`
- `security.md`

Removed as stale or redundant:

- `retrieval.md` — too thin and superseded by architecture/product-proof until a real retrieval design doc is written.
- `agent_workflow.md` — too thin and superseded by architecture/deployment workflow notes.
- `evals.md` — too thin and not useful without a proper benchmark/eval strategy doc.
- `collector_protocol.md` — redundant with `collector-core-contract.md`.
- `sample_output.md` — stale sample, replaced by product proof and actual benchmark/report expectations.
- `demo_script.md` — stale local/demo flow, replaced by cloud-only user E2E runbook.

## Accuracy notes

- Core CI/CD lives in `.github/workflows/deploy-azure.yml`.
- The Core workflow currently builds and deploys Core plus Collector. Frontend infrastructure exists in Azure, but the Core workflow leaves frontend build/deploy disabled by default.
- Runtime truth comes from `/v1/runtime/status`, not from docs or screenshots.
- Large-repo benchmark numbers in `temporal-benchmark.md` are failure reports until a clean post-fix rerun is published.

If those topics need dedicated docs later, recreate them with real detail and current Azure assumptions. Do not revive tiny placeholder docs merely so the docs folder can cosplay as comprehensive. Tiny docs lie by omission, which is rude even by software standards.
