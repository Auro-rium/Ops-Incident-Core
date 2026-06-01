# IncidentOps MCP Architecture

IncidentOps uses MCP as an interface layer over Core, not as an ingestion path and not as a second brain.

## Decision

The product MCP surface is **Core MCP**.

There is no supported Collector MCP surface in the current Collector codebase. Collector does not ship an MCP package extra or server entrypoint.

```text
External AI client
  -> Core MCP server
  -> Core API
  -> readiness / search / investigate / sync / workflow endpoints
  -> cited Core response
```

## Why Core MCP Is the Product MCP

Core owns the user-facing intelligence capabilities:

- readiness reports
- cited search
- cited answer generation
- incident investigation
- sync and source status
- workflow run events
- project-scoped auth/RBAC

External AI clients should call these capabilities through Core MCP. That gives Claude, Cursor, Codex, or another MCP-capable client a clean way to ask IncidentOps for evidence-backed answers.

Core MCP tools:

- `get_capabilities`
- `get_readiness_report`
- `search_evidence`
- `investigate_incident`
- `get_sync_status`
- `get_latest_source_sync`
- `get_run_events`

## Core MCP Rules

Core MCP must:

- call Core APIs only
- use a scoped Core token from Azure secrets / Key Vault wiring
- respect Core JWT/RBAC boundaries
- return cited evidence and missing-data warnings
- log tool calls without secrets
- preserve insufficient-evidence behavior

Core MCP must not:

- ingest files
- normalize files
- read local repositories
- call the database directly
- bypass Core auth/RBAC
- mutate external systems
- diagnose locally outside Core

## Collector Boundary

Collector remains a CLI/daemon/runtime boundary, not an MCP surface:

- inspect local repo paths
- preview collection
- validate config
- run approved sync to Core

Those behaviors stay in Collector CLI and daemon flows. The Azure product and external AI client story uses Core MCP only.

## Azure Deployment

The Azure deployment should run a separate Core MCP Container App:

```text
incidentops-mcp
```

Expected runtime settings:

```text
MCP_TRANSPORT=streamable-http
MCP_HOST=0.0.0.0
MCP_PORT=8080
MCP_PATH=/mcp
MCP_CORE_API_URL=https://<core-api-fqdn>
MCP_TOKEN=<scoped Core token>
```

Recommended ingress posture:

- private/internal by default
- protected/authenticated if exposed
- never public unauthenticated

## End-to-End MCP Proof

The product proof is:

```text
Collector syncs evidence into Core
  -> Core readiness report works
  -> Core search returns cited evidence
  -> Core investigate returns cited answer or insufficient-evidence response
  -> Core MCP tools expose the same capabilities to an AI client
```

MCP is not successful because a server starts. MCP is successful when an external AI client calls Core-backed tools and receives scoped, cited, useful evidence. Starting servers and celebrating is how civilization got dashboards nobody reads.
