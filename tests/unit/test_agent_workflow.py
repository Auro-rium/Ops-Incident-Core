from __future__ import annotations

from incidentops.agent.nodes import classify_task_node, extract_entities_node, finalize_answer_node, resolve_scope_node
from incidentops.agent.state import InvestigationState


def test_agent_workflow_handles_generic_query():
    state = InvestigationState(run_id="r1", project_id="p1", user_query="Why did GET /v1/orders slow down after deploy abc1234?")
    state = classify_task_node(state)
    state = extract_entities_node(state)
    state = resolve_scope_node(state)
    state.evidence = [{"citation": {"label": "[1]", "path": "logs/api.log", "lines": "1-3"}, "source_type": "logs", "text": "timeout after 1500ms"}]
    state.log_findings = {"services": ["orders-api"]}
    state.selected_root_cause = {"hypothesis_id": "weak_evidence", "summary": "Evidence is insufficient to identify a confident root cause.", "confidence": "low"}
    state = finalize_answer_node(state)
    assert state.task_type == "latency_investigation"
    assert state.entities["deploy_hash"] == "abc1234"
    assert state.final_answer["likely_root_cause"]["confidence"] == "low"
