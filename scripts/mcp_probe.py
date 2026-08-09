"""Run a bounded MCP probe from inside the Azure Container Apps network."""

from __future__ import annotations

import asyncio
import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _result_json(result) -> dict:
    text = "".join(getattr(item, "text", "") for item in (getattr(result, "content", None) or []))
    if not text:
        return {}
    payload = json.loads(text)
    return payload if isinstance(payload, dict) else {}


async def _probe() -> None:
    project_id = os.environ["PROJECT_ID"]
    mcp_url = os.environ["MCP_URL"]
    query = os.environ.get("MCP_PROBE_QUERY", "Where is FastAPI used?")
    token = os.environ.get("MCP_TOKEN", "").strip()
    if not token:
        raise RuntimeError("MCP_TOKEN is required")

    async with streamablehttp_client(
        mcp_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
        sse_read_timeout=20,
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool_names = sorted(tool.name for tool in (await session.list_tools()).tools)
            required = {"get_capabilities", "get_readiness_report", "search_evidence", "investigate_incident"}
            missing = sorted(required - set(tool_names))
            if missing:
                raise RuntimeError(f"MCP tools missing: {missing}")

            capabilities = _result_json(await session.call_tool("get_capabilities", {}))
            readiness = _result_json(
                await session.call_tool("get_readiness_report", {"project_id": project_id})
            )
            search = _result_json(
                await session.call_tool(
                    "search_evidence", {"project_id": project_id, "query": query, "top_k": 5}
                )
            )
            investigation = _result_json(
                await session.call_tool(
                    "investigate_incident", {"project_id": project_id, "query": query, "top_k": 5}
                )
            )

            print(
                json.dumps(
                    {
                        "tools": tool_names,
                        "core_version": capabilities.get("version"),
                        "readiness_score": readiness.get("score"),
                        "search_result_count": int(search.get("total", 0) or 0),
                        "search_paths": [
                            item.get("document_path") for item in search.get("results", [])[:5]
                        ],
                        "investigation_citation_count": len(investigation.get("citations", [])),
                    },
                    sort_keys=True,
                )
            )


async def main() -> None:
    timeout_seconds = max(1.0, float(os.environ.get("MCP_PROBE_TIMEOUT_SECONDS", "45")))
    try:
        await asyncio.wait_for(_probe(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"MCP probe timed out after {timeout_seconds:.0f}s") from exc


if __name__ == "__main__":
    asyncio.run(main())
