# Documentation Index

This folder contains the current documentation for IncidentOps Core.

The active product story is Azure-first:

```text
Collector -> Core API -> PostgreSQL/pgvector -> Azure OpenAI -> readiness/search/investigate -> Core MCP -> frontend
```

Local execution is kept only for deterministic development and CI safety. It is not the flagship product proof path.

## Current Documentation Map

- [Architecture](./architecture.md) — system topology, component responsibilities, Azure runtime split, and failure modes.
- [Azure deployment](./azure-deployment.md) — Azure Container Apps, ACR, PostgreSQL, Redis, Key Vault, Log Analytics, Azure OpenAI, and CI/CD.
- [Azure cost guardrails](./azure-cost-guardrails.md) — budget alerts, teardown, and cloud cost risks.
- [Cloud-only user E2E runbook](./user-e2e-runbook.md) — real Azure user proof with benchmark ingestion, runtime status, and MCP smoke.
- [Product proof](./product-proof.md) — what counts as product proof, current metrics, and what not to overclaim.
- [Temporal benchmark](./temporal-benchmark.md) — current beast-repo benchmark, first-run bottleneck, and rerun criteria.
- [MCP architecture](./mcp-architecture.md) — Core MCP as the product MCP and Collector MCP as local/private operator tooling only.
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

If those topics need dedicated docs later, recreate them with real detail and current Azure assumptions. Do not revive tiny placeholder docs merely so the docs folder can cosplay as comprehensive. Tiny docs lie by omission, which is rude even by software standards.
