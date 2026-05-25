#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AZURE_RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-incidentops-demo-swc-rg}"
MCP_APP_NAME="${MCP_APP_NAME:-incidentops-mcp}"
PROJECT_ID="${PROJECT_ID:-${INCIDENTOPS_PROJECT_ID:-}}"
SEARCH_QUERY="${SEARCH_QUERY:-Where is the history service implemented?}"
INVESTIGATE_QUERY="${INVESTIGATE_QUERY:-Which parts of the Temporal repo are relevant to investigating workflow task latency?}"
MCP_URL="${MCP_URL:-http://127.0.0.1:8080/mcp}"

"$ROOT_DIR/scripts/azure_login_check.sh"

if [[ -z "$PROJECT_ID" ]]; then
  echo "PROJECT_ID or INCIDENTOPS_PROJECT_ID is required for readiness/search/investigation MCP tool smoke." >&2
  exit 1
fi

PYTHON_CODE_B64="$(python3 - <<'PY'
import base64
code = r'''
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

project_id = os.environ["INCIDENTOPS_PROJECT_ID"]
search_query = os.environ.get("SEARCH_QUERY", "Where is the history service implemented?")
investigate_query = os.environ.get("INVESTIGATE_QUERY", "Which parts of the Temporal repo are relevant to investigating workflow task latency?")
mcp_url = os.environ.get("MCP_URL", "http://127.0.0.1:8080/mcp")


def _content_text(result: Any) -> str:
    content = getattr(result, "content", None) or []
    parts: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _json_from_result(result: Any) -> Any:
    text = _content_text(result)
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text[:1000]}


async def main() -> None:
    async with streamablehttp_client(mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            tool_names = sorted(tool.name for tool in listed.tools)
            required = {"get_capabilities", "get_readiness_report", "search_evidence", "investigate_incident", "get_latest_source_sync", "get_sync_status", "get_run_events"}
            missing = sorted(required - set(tool_names))
            if missing:
                raise SystemExit(f"missing MCP tools: {missing}")

            capabilities = _json_from_result(await session.call_tool("get_capabilities", {}))
            readiness = _json_from_result(await session.call_tool("get_readiness_report", {"project_id": project_id}))
            search = _json_from_result(await session.call_tool("search_evidence", {"project_id": project_id, "query": search_query, "top_k": 5}))
            investigation = _json_from_result(await session.call_tool("investigate_incident", {"project_id": project_id, "query": investigate_query, "top_k": 5}))

            search_count = int(search.get("total", len(search.get("results", []) or [])) or 0)
            citation_count = len(investigation.get("citations") or investigation.get("evidence") or [])
            summary = {
                "mcp_url": mcp_url,
                "tools": tool_names,
                "core_version": capabilities.get("version"),
                "readiness_score": readiness.get("score"),
                "search_result_count": search_count,
                "investigation_citation_count": citation_count,
            }
            print(json.dumps(summary, indent=2, sort_keys=True))
            if search_count <= 0:
                raise SystemExit("MCP search_evidence returned zero results")

asyncio.run(main())
'''
print(base64.b64encode(code.encode()).decode())
PY
)"

COMMAND="INCIDENTOPS_PROJECT_ID='${PROJECT_ID}' SEARCH_QUERY='${SEARCH_QUERY}' INVESTIGATE_QUERY='${INVESTIGATE_QUERY}' MCP_URL='${MCP_URL}' python -c \"import base64; exec(base64.b64decode('${PYTHON_CODE_B64}'))\""

echo "Running MCP smoke inside Container App '$MCP_APP_NAME'..."
az containerapp exec \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$MCP_APP_NAME" \
  --command "/bin/sh -lc $COMMAND" \
  --only-show-errors
