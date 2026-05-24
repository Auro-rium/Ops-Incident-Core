from __future__ import annotations

import logging
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from incidentops.config.settings import get_settings

logger = logging.getLogger("incidentops.mcp")
settings = get_settings()

mcp = FastMCP(
    "IncidentOps Core",
    instructions=(
        "Use these tools to inspect IncidentOps Core evidence, readiness, investigation, "
        "sync status, and workflow run events. The server delegates to Core APIs and "
        "does not ingest or normalize data."
    ),
    host=settings.mcp_host,
    port=settings.mcp_port,
    streamable_http_path=settings.mcp_path,
    stateless_http=True,
)


def _headers() -> dict[str, str]:
    token = settings.mcp_token.strip()
    if not token or token == "disabled":
        raise RuntimeError("MCP_TOKEN or INCIDENTOPS_MCP_TOKEN is required")
    return {"Authorization": f"Bearer {token}"}


async def _request(method: str, path: str, *, json: dict[str, Any] | None = None) -> dict[str, Any] | list[Any]:
    base_url = settings.mcp_core_api_url.rstrip("/")
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0, headers=_headers()) as client:
        response = await client.request(method, path, json=json)
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def get_capabilities() -> dict[str, Any]:
    """Return Core feature, endpoint, and limit capabilities."""
    base_url = settings.mcp_core_api_url.rstrip("/")
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        response = await client.get("/v1/capabilities")
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def get_readiness_report(project_id: str) -> dict[str, Any]:
    """Return project readiness, coverage, missing evidence, and suggested questions."""
    return await _request("GET", f"/v1/projects/{project_id}/readiness")


@mcp.tool()
async def search_evidence(project_id: str, query: str, top_k: int = 8) -> dict[str, Any]:
    """Search cited engineering evidence in a project."""
    return await _request(
        "POST",
        "/v1/search",
        json={"project_id": project_id, "query": query, "top_k": top_k},
    )


@mcp.tool()
async def investigate_incident(project_id: str, query: str, top_k: int = 8) -> dict[str, Any]:
    """Run an IncidentOps cited investigation over retrieved evidence."""
    return await _request(
        "POST",
        "/v1/investigate",
        json={"project_id": project_id, "query": query, "top_k": top_k},
    )


@mcp.tool()
async def get_sync_status(sync_id: str) -> dict[str, Any]:
    """Return a source sync status and diagnostics."""
    payload = await _request("GET", f"/v1/syncs/{sync_id}")
    if isinstance(payload, list):
        return {"items": payload}
    return payload


@mcp.tool()
async def get_latest_source_sync(source_id: str) -> dict[str, Any]:
    """Return the latest sync for a source."""
    payload = await _request("GET", f"/v1/sources/{source_id}/syncs/latest")
    if isinstance(payload, list):
        return {"items": payload}
    return payload


@mcp.tool()
async def get_run_events(run_id: str) -> list[dict[str, Any]]:
    """Return workflow run events for an IncidentOps run."""
    payload = await _request("GET", f"/v1/runs/{run_id}/events")
    if isinstance(payload, list):
        return payload
    return [payload]


def main() -> None:
    transport = settings.mcp_transport.strip().lower()
    if transport not in {"stdio", "sse", "streamable-http"}:
        raise RuntimeError("MCP_TRANSPORT must be one of: stdio, sse, streamable-http")
    logger.info("Starting IncidentOps MCP server with %s transport", transport)
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
