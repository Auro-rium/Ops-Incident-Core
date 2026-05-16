# Documentation Hub

Central index for all project documentation.

## Documentation Map

| Document | Topic | Audience | Purpose |
| --- | --- | --- | --- |
| [Architecture](./architecture.md) | System components, boundaries, and runtime behavior | Operator, Developer, SRE | Understand how platform services fit together and where responsibilities live |
| [Deployment](./deployment.md) | Environment setup, boot sequence, and release operations | Operator, SRE | Deploy and operate the system safely in production |
| [Security](./security.md) | Auth, RBAC, trust boundaries, and hardening controls | Security Reviewer, Developer, SRE | Review and enforce security controls and assumptions |
| [Retrieval](./retrieval.md) | Retrieval pipeline and evidence selection | Developer, Operator | Explain how evidence is sourced, ranked, and passed to agents |
| [Agent Workflow](./agent_workflow.md) | Run lifecycle, approvals, and workflow states | Operator, Developer | Operate and extend incident investigation workflows |
| [Evals](./evals.md) | Golden cases and quality measurement process | Developer, Operator, SRE | Validate regression risk and answer quality over time |
| [Collector Protocol](./collector_protocol.md) | Data ingestion contracts and collector behavior | Developer, Operator, Security Reviewer | Implement and review ingestion interfaces and trust model |
| [Demo Script](./demo_script.md) | Demo walkthrough for product flow | Operator, Developer | Run a consistent product demonstration |
| [Sample Output](./sample_output.md) | Example investigation output formatting | Operator, Developer | Reference expected answer format and structure |

## Audience and Purpose Tags

Tag legend used across docs:

- **Operator**: Runs workflows and day-to-day incident processes.
- **Developer**: Builds or modifies product features and integrations.
- **Security Reviewer**: Assesses control coverage, trust boundaries, and risk.
- **SRE**: Owns reliability, deployment safety, observability, and rollback readiness.

## Diagrams & Images Standard

1. **Mermaid source location**
   - Keep Mermaid diagram source inline in the owning Markdown document.
   - Place each Mermaid block near the section it documents.
2. **Exported static assets**
   - Store rendered or hand-authored static images in `docs/assets/`.
   - Prefer SVG for architecture and sequence diagrams; use PNG for screenshots and raster artifacts.
3. **File naming convention**
   - Format: `<domain>-<subject>-v<version>.<ext>`
   - Examples:
     - `arch-container-v1.svg`
     - `security-auth-flow-v1.png`
     - `retrieval-ranking-pipeline-v2.svg`
4. **Architecture-change update process**
   - Update the relevant Markdown narrative and inline Mermaid first.
   - Re-export static image assets to `docs/assets/` with incremented version suffix when diagram meaning changes.
   - Replace image links in the owning doc to the latest version.
   - Keep at least one prior version when useful for diff/audit context, otherwise remove stale assets.
   - Validate cross-links from this hub and impacted docs after updates.

## Docs Quality Gate

Before merging documentation changes, verify:

- [ ] Every production-facing doc includes **at least one diagram** (Mermaid and/or static image).
- [ ] Every critical configuration variable lists **default**, **required/optional**, and **environment scope**.
- [ ] Every operational process includes explicit **rollback guidance**.
