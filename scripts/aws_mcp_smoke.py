from __future__ import annotations

import asyncio
import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main() -> None:
    mcp_url = os.environ["MCP_URL"]
    project_id = os.environ["INCIDENTOPS_PROJECT_ID"]
    async with streamablehttp_client(mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = sorted(tool.name for tool in tools.tools)
            required = {"get_capabilities", "get_readiness_report", "search_evidence", "investigate_incident"}
            missing = sorted(required - set(tool_names))
            if missing:
                raise RuntimeError(f"missing MCP tools: {missing}")
            capabilities = await session.call_tool("get_capabilities", {})
            readiness = await session.call_tool("get_readiness_report", {"project_id": project_id})
            search = await session.call_tool(
                "search_evidence",
                {"project_id": project_id, "query": "Where is request routing implemented?", "top_k": 5},
            )
            investigation = await session.call_tool(
                "investigate_incident",
                {"project_id": project_id, "query": "What evidence is available for request latency?", "top_k": 5},
            )
            failed = [name for name, result in {
                "capabilities": capabilities,
                "readiness": readiness,
                "search": search,
                "investigation": investigation,
            }.items() if result.isError]
            if failed:
                raise RuntimeError(f"MCP tool calls failed: {failed}")
            print(json.dumps({"mcp_tools": tool_names, "calls_succeeded": 4}, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
