from __future__ import annotations

import hmac
import logging
import uuid
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


def _client() -> httpx.AsyncClient:
    """Outbound client to Core, authenticated with the service MCP_TOKEN."""
    return httpx.AsyncClient(
        base_url=settings.mcp_core_api_url.rstrip("/"),
        timeout=30.0,
        headers=_headers(),
    )


async def _request(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | list[Any]:
    async with _client() as client:
        response = await client.request(method, path, json=json, params=params)
        response.raise_for_status()
        return response.json()


class BearerTokenMiddleware:
    """Pure ASGI middleware requiring ``Authorization: Bearer <token>`` on HTTP requests.

    Written without BaseHTTPMiddleware so SSE and streamable HTTP responses
    are passed through untouched.
    """

    def __init__(self, app, token: str):
        if not token.strip():
            raise RuntimeError("MCP_INBOUND_TOKEN is required for HTTP MCP transports")
        self.app = app
        self._expected = token.strip().encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if self._authorized(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b'{"detail":"Unauthorized"}'})

    def _authorized(self, scope) -> bool:
        for name, value in scope.get("headers") or []:
            if name.lower() != b"authorization":
                continue
            scheme, _, token = value.partition(b" ")
            if scheme.lower() != b"bearer" or not token.strip():
                return False
            return hmac.compare_digest(token.strip(), self._expected)
        return False


@mcp.tool()
async def get_capabilities() -> dict[str, Any]:
    """Return Core feature, endpoint, and limit capabilities."""
    return await _request("GET", "/v1/capabilities")


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


@mcp.tool()
async def get_eval_summary(project_id: str) -> dict[str, Any]:
    """Return the latest persisted deterministic evaluation summary for a project."""
    try:
        parsed_project_id = uuid.UUID(project_id)
    except (TypeError, ValueError):
        return {"status": "invalid_project_id", "project_id": project_id}
    payload = await _request("GET", "/v1/evals", params={"project_id": str(parsed_project_id)})
    if not isinstance(payload, list):
        return {"status": "unavailable"}
    if not payload:
        return {"status": "no_eval_runs", "project_id": str(parsed_project_id)}
    latest = payload[0]
    return {
        "project_id": str(parsed_project_id),
        "eval_run_id": latest.get("eval_run_id"),
        "status": latest.get("status"),
        "summary": latest.get("summary") or {},
    }


def build_http_app(transport: str):
    """Return the MCP ASGI app for an HTTP transport, wrapped in inbound bearer auth."""
    if transport == "streamable-http":
        app = mcp.streamable_http_app()
    elif transport == "sse":
        app = mcp.sse_app()
    else:
        raise RuntimeError(f"{transport} is not an HTTP MCP transport")
    return BearerTokenMiddleware(app, settings.mcp_inbound_token)


def main() -> None:
    transport = settings.mcp_transport.strip().lower()
    if transport not in {"stdio", "sse", "streamable-http"}:
        raise RuntimeError("MCP_TRANSPORT must be one of: stdio, sse, streamable-http")
    logger.info("Starting IncidentOps MCP server with %s transport", transport)
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    import uvicorn

    uvicorn.run(build_http_app(transport), host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
