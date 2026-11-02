from __future__ import annotations

from types import SimpleNamespace

import pytest

from incidentops.agent import nodes
from incidentops.agent import service as agent_service
from incidentops.agent.graph import NODE_ORDER, route_after_risk_gate, run_agent_graph
from incidentops.agent.state import InvestigationState
from incidentops.db.models import IncidentReportDraft, IssueDraft


def _state(confidence: str = "medium", **kwargs) -> InvestigationState:
    state = InvestigationState(run_id="r1", project_id="p1", user_query="Why did checkout slow down?", **kwargs)
    state.selected_root_cause = {"hypothesis_id": "h1", "summary": "Synchronous inventory call", "confidence": confidence}
    return state


def _complete_evidence(state: InvestigationState) -> InvestigationState:
    state.deploy_findings = {"diff_count": 1}
    state.log_findings = {"timestamped_logs": 3}
    state.previous_incidents = [{"id": "inc"}]
    state.evidence = [{"source_type": "runbook", "citation": {}}]
    return state


def test_risk_gate_skips_approval_without_issue_draft():
    state = nodes.risk_gate_node(_state("low"))
    assert state.pending_approval is False
    assert state.risk_level in {"medium", "high"}


def test_risk_gate_requires_approval_for_risky_issue_draft():
    state = _state("low")
    state.issue_draft = {"title": "t", "body": "b"}
    state = nodes.risk_gate_node(state)
    assert state.pending_approval is True
    assert state.risk_level in {"medium", "high"}


def test_risk_gate_low_risk_issue_draft_does_not_need_approval():
    state = _complete_evidence(_state("high"))
    state.issue_draft = {"title": "t", "body": "b"}
    state = nodes.risk_gate_node(state)
    assert state.risk_level == "low"
    assert state.pending_approval is False


def test_drafts_are_built_after_answer_content():
    state = nodes.compose_answer_node(_state("medium"))
    state = nodes.draft_issue_node(state)
    state = nodes.draft_incident_report_node(state)
    fix = state.final_answer["suggested_fix"]
    assert fix
    assert fix in state.issue_draft["body"]
    assert fix in state.incident_report_draft["markdown"]
    assert [name for name, _ in NODE_ORDER].index("compose_answer") < [name for name, _ in NODE_ORDER].index("draft_issue")


def test_issue_draft_skipped_when_disabled():
    state = nodes.compose_answer_node(_state("low", create_issue_draft=False))
    state = nodes.draft_issue_node(state)
    assert state.issue_draft == {}


def test_resolve_scope_reuses_extracted_entities(monkeypatch):
    def fail(_query):
        raise AssertionError("entities must not be re-extracted")

    monkeypatch.setattr(nodes, "extract_entities", fail)
    state = _state()
    state.entities = {"service_name": "checkout", "endpoint": "/checkout", "deploy_hash": "8f13a2", "symptom": None, "time_window": None}
    state = nodes.resolve_scope_node(state)
    assert state.retrieval_plan["filters"]["endpoint"] == "/checkout"


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.asyncio
async def test_fallback_graph_skips_wait_node_unless_approval_pending(pending):
    visited: list[str] = []

    async def runner(name, node, state):
        visited.append(name)
        if name == "risk_gate":
            state.pending_approval = pending
            return state
        return node(state)

    result = await run_agent_graph(_state(), node_runner=runner, prefer_langgraph=False)

    assert ("wait_for_human_approval" in visited) is pending
    assert visited[-1] == "finalize_answer"
    assert result.status == ("awaiting_approval" if pending else "completed")
    assert route_after_risk_gate(result) == ("wait_for_human_approval" if pending else "finalize_answer")


class DraftDb:
    def __init__(self) -> None:
        self.added: list = []
        self.flushes = 0

    async def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1


@pytest.mark.asyncio
async def test_persist_drafts_always_flushes_and_respects_issue_flag():
    state = _state(create_issue_draft=False)
    state.incident_report_draft = {"markdown": "# r"}
    state.issue_draft = {"title": "t", "body": "b"}
    db = DraftDb()

    await agent_service._persist_drafts(db, "run", state)

    assert db.flushes == 1
    assert [type(obj) for obj in db.added] == [IncidentReportDraft]
    assert not any(isinstance(obj, IssueDraft) for obj in db.added)
