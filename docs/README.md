# Documentation Index

This folder contains technical documentation for IncidentOps Core.

## Documentation Map

- [Architecture](./architecture.md) — system topology, component responsibilities, data flows, failure modes.
- [Deployment](./deployment.md) — Azure production boot sequence, secure configuration, runtime operations.
- [Azure Deployment](./azure-deployment.md) — Azure Container Apps, ACR, PostgreSQL, Redis, Key Vault, Log Analytics, CI/CD.
- [Azure Cost Guardrails](./azure-cost-guardrails.md) — budget alerts, teardown, cost risks.
- [Security](./security.md) — threat model, trust boundaries, control matrix, incident response and verification.
- [Retrieval](./retrieval.md) — indexing/search design and evidence retrieval behavior.
- [Agent Workflow](./agent_workflow.md) — deterministic execution graph, approvals, event persistence.
- [Evals](./evals.md) — evaluation strategy and runtime behavior.
- [Collector Protocol](./collector_protocol.md) — collector/sync/document ingestion APIs and constraints.
- [Collector/Core Contract](./collector-core-contract.md) — versioned Core contract used by Collector.
- [Sample Output](./sample_output.md) — representative answer/report format.
- [Demo Script](./demo_script.md) — product demonstration walkthrough.

## Audience & Purpose

| Doc | Primary audience | Purpose |
|---|---|---|
| architecture.md | Developers, SREs | Understand end-to-end system design and reliability behavior |
| deployment.md | SREs, platform engineers | Operate safely in Azure staging/production |
| azure-deployment.md | Platform engineers | Provision and deploy Azure resources |
| azure-cost-guardrails.md | Operators | Avoid accidental Azure spend |
| security.md | Security reviewers, operators | Validate controls and run security operations |
| retrieval.md | Developers, ML engineers | Understand retrieval mechanics and constraints |
| agent_workflow.md | Developers, operators | Understand workflow nodes and approval mechanics |
| evals.md | ML engineers, QA | Evaluate and track quality regression |
| collector_protocol.md | Integrators | Implement compliant ingestion clients |
