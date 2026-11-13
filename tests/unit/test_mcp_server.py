from __future__ import annotations

import json
import uuid

import httpx
import pytest

from incidentops.mcp import server


@pytest.fixture
def core(monkeypatch):
    """Mock Core API: records requests and serves canned JSON."""
    seen: list[httpx.Request] = []
    routes: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        payload = routes.get(request.url.path, {"ok": True})
        return httpx.Response(200, json=payload)

    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://core.test",
            headers=server._headers(),
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(server.settings, "mcp_token", "core-service-token")
    monkeypatch.setattr(server, "_client", _client)
    return seen, routes


async def test_get_capabilities_uses_outbound_service_token(core):
    seen, routes = core
    routes["/v1/capabilities"] = {"version": "0.5.0"}
    result = await server.get_capabilities()
    assert result == {"version": "0.5.0"}
    assert seen[0].headers["authorization"] == "Bearer core-service-token"


async def test_search_evidence_posts_query(core):
    seen, _routes = core
    project_id = str(uuid.uuid4())
    await server.search_evidence(project_id, "why is latency high", top_k=3)
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/search"
    assert json.loads(request.content) == {"project_id": project_id, "query": "why is latency high", "top_k": 3}
    assert request.headers["authorization"] == "Bearer core-service-token"


async def test_get_eval_summary_filters_server_side(core):
    seen, routes = core
    project_id = str(uuid.uuid4())
    routes["/v1/evals"] = [
        {"eval_run_id": "e1", "project_id": project_id, "status": "completed", "summary": {"recall": 0.9}}
    ]
    result = await server.get_eval_summary(project_id)
    assert seen[0].url.params["project_id"] == project_id
    assert result["eval_run_id"] == "e1"
    assert result["summary"] == {"recall": 0.9}


async def test_get_eval_summary_handles_empty_and_invalid(core):
    seen, routes = core
    routes["/v1/evals"] = []
    project_id = str(uuid.uuid4())
    assert (await server.get_eval_summary(project_id))["status"] == "no_eval_runs"
    assert (await server.get_eval_summary("not-a-uuid"))["status"] == "invalid_project_id"
    assert len(seen) == 1


async def test_outbound_requires_service_token(monkeypatch):
    monkeypatch.setattr(server.settings, "mcp_token", "")
    with pytest.raises(RuntimeError):
        server._headers()


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def test_inbound_bearer_middleware_rejects_missing_and_wrong_tokens():
    app = server.BearerTokenMiddleware(_ok_app, "inbound-secret")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp.test") as client:
        assert (await client.post("/mcp")).status_code == 401
        assert (await client.post("/mcp", headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await client.post("/mcp", headers={"Authorization": "Basic inbound-secret"})).status_code == 401
        ok = await client.post("/mcp", headers={"Authorization": "Bearer inbound-secret"})
        assert ok.status_code == 200 and ok.text == "ok"


def test_inbound_middleware_fails_closed_without_token():
    with pytest.raises(RuntimeError):
        server.BearerTokenMiddleware(_ok_app, "")


async def test_http_app_requires_inbound_token(monkeypatch):
    monkeypatch.setattr(server.settings, "mcp_inbound_token", "")
    with pytest.raises(RuntimeError):
        server.build_http_app("streamable-http")
    monkeypatch.setattr(server.settings, "mcp_inbound_token", "inbound-secret")
    app = server.build_http_app("streamable-http")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp.test") as client:
        response = await client.post(server.settings.mcp_path, json={})
    assert response.status_code == 401
