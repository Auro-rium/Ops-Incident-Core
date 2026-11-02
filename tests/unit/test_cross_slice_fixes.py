from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from apps.api.routes import runs as runs_route
from incidentops.agent import nodes
from incidentops.agent.state import InvestigationState, state_from_graph_dict, state_to_graph_dict
from incidentops.collector.client import CoreCollectorClient
from incidentops.config.settings import Settings
from incidentops.db.models import ProjectRole
from incidentops.ingestion.diagnostics import inspect_folder
from incidentops.ingestion.indexer import _parse_normalized_document
from incidentops.schemas.api import IngestResponse


class _Result:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Session:
    def __init__(self, value) -> None:
        self._value = value

    async def execute(self, _statement):
        return _Result(self._value)


@pytest.mark.asyncio
async def test_approval_requires_approver_role_even_when_demo_bypass_grants_access(monkeypatch):
    run = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4())
    required: list = []

    async def bypassed_access(*_args, **_kwargs):
        return None

    async def deny_role(_db, project_id, _user_id, minimum_role):
        required.append((project_id, minimum_role))
        raise HTTPException(status_code=403, detail="Insufficient project role")

    async def must_not_resume(*_args, **_kwargs):
        raise AssertionError("approval ran without the approver role")

    monkeypatch.setattr(runs_route, "ensure_project_access", bypassed_access)
    monkeypatch.setattr(runs_route, "require_project_role", deny_role, raising=False)
    monkeypatch.setattr(runs_route, "resume_after_approval", must_not_resume)

    with pytest.raises(HTTPException) as exc_info:
        await runs_route.approve_run(
            run.id,
            SimpleNamespace(rationale=None),
            None,
            db=_Session(run),
            settings=Settings(),
            user=SimpleNamespace(id=uuid.uuid4()),
        )

    assert exc_info.value.status_code == 403
    assert required == [(run.project_id, ProjectRole.approver)]


def test_workflow_entities_use_known_services_and_survive_graph_state():
    state = InvestigationState(
        run_id="run",
        project_id="project",
        user_query="why is checkout-api slow",
        known_services=["checkout-api"],
    )
    state = state_from_graph_dict(state_to_graph_dict(state))

    nodes.extract_entities_node(state)

    assert state.entities["service_name"] == "checkout-api"


def test_inspect_folder_applies_shared_discovery_rules(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("module.exports = 1\n")
    (tmp_path / "app.py").write_text("print('ok')\n")
    (tmp_path / "Makefile").write_text("all:\n")
    (tmp_path / "server.pem").write_text("key\n")
    (tmp_path / ".netrc").write_text("machine x\n")

    report = inspect_folder(str(tmp_path), Settings())

    assert report["ingestable_files"] == 2
    assert report["total_files_seen"] == 4
    assert {item["path"] for item in report["unsupported_files"]} == {"server.pem", ".netrc"}


def test_ingest_response_exposes_sync_status():
    assert "status" in IngestResponse.model_fields


@pytest.mark.parametrize(
    ("diagnostics", "expected"),
    [({"complete": True}, True), ({"complete": False}, False), ({}, False)],
)
def test_collector_sends_top_level_complete_from_diagnostics(diagnostics, expected):
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = CoreCollectorClient("http://core", "token")
    client._client = httpx.Client(base_url="http://core", transport=httpx.MockTransport(handler))

    client.finish_sync("source", "sync", "collector", "success", diagnostics)

    assert sent[0]["complete"] is expected


def test_json_named_like_a_deploy_only_by_substring_is_not_parsed_as_deploy_history():
    document = SimpleNamespace(path="docs/committee.json", content='{"members": ["a"]}', source_type="config")

    _chunks, error = _parse_normalized_document(document, service_name=None)

    assert error is None
