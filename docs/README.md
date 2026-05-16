# Documentation Index

This folder contains production-facing technical documentation for IncidentOps.

## Documentation Map

- [Architecture](./architecture.md) — system topology, component responsibilities, data flows, failure modes.
- [Deployment](./deployment.md) — production boot sequence, secure configuration, runtime operations, SLO-oriented checks.
- [Security](./security.md) — threat model, trust boundaries, control matrix, incident response and verification.
- [Retrieval](./retrieval.md) — indexing/search design and evidence retrieval behavior.
- [Agent Workflow](./agent_workflow.md) — deterministic execution graph, approvals, event persistence.
- [Evals](./evals.md) — evaluation strategy and runtime behavior.
- [Collector Protocol](./collector_protocol.md) — collector/sync/document ingestion APIs and constraints.
- [Sample Output](./sample_output.md) — representative answer/report format.
- [Demo Script](./demo_script.md) — local/demo run-through.

## Audience & Purpose

| Doc | Primary audience | Purpose |
|---|---|---|
| architecture.md | Developers, SREs | Understand end-to-end system design and reliability behavior |
| deployment.md | SREs, platform engineers | Operate safely in staging/production |
| security.md | Security reviewers, operators | Validate controls and run security operations |
| retrieval.md | Developers, ML engineers | Understand retrieval mechanics and constraints |
| agent_workflow.md | Developers, operators | Understand workflow nodes and approval mechanics |
| evals.md | ML engineers, QA | Evaluate and track quality regression |
| collector_protocol.md | Integrators | Implement compliant ingestion clients |

## Diagrams & Images Standard

- Mermaid diagrams are the source of truth and should be maintained inline in each markdown file.
- Static rendered images live under `docs/assets/` for consumers that cannot render Mermaid.
- Use versioned names like:
  - `arch-container-v1.svg`
  - `deploy-topology-v1.svg`
  - `security-auth-flow-v1.svg`
- When architecture/security/deployment behavior changes:
  1. Update Mermaid source in docs.
  2. Regenerate/update matching `docs/assets/*` image.
  3. Update version suffix when the structure materially changes.

## Docs Quality Gate

Before merging docs updates, verify:

- Every production-facing doc includes at least one diagram.
- Every critical config variable states environment expectations and failure behavior.
- Every operational workflow includes rollback or recovery guidance.
- Security-sensitive flows include explicit trust boundaries and audit signals.

