# IncidentOps MCP Architecture

IncidentOps uses MCP as an interface layer over Core, not as an ingestion path and not as a second brain. This boundary matters because otherwise every useful system eventually becomes a haunted tool loop wearing a product name.

## Decision

The product MCP surface is **Core MCP**.

Collector MCP, if used, is local/private operator tooling only. It is not deployed as the public product MCP surface.

```text
External AI client
  -> Core MCP server
  -> Core API
  -> readiness / search / investigate / sync / workflow endpoints
  -> cited Core response
```

```text
Local operator only, optional
  -> Collector MCP
  -> inspect local paths / preview collection / validate config
  -> approved Collector sync
  -> Core batch ingest API
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

## Collector MCP Boundary

Collector MCP is only useful for controlled local/operator workflows:

- inspect a local repo path
- preview what would be collected
- validate source config
- preview redaction
- optionally trigger approved sync to Core

Collector MCP must stay:

- private/local by default
- path-policy protected
- approval-gated for sync/export actions
- redaction-safe
- unable to bypass Core ingestion contracts

Do not expose Collector MCP publicly in Azure. For the product demo and external AI client story, use Core MCP only.

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
