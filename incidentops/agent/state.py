from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, TypedDict


class InvestigationGraphState(TypedDict, total=False):
    """LangGraph state schema for the IncidentOps investigation workflow.

    LangGraph nodes pass dictionaries between graph steps. The existing
    workflow code uses ``InvestigationState`` as a dataclass, so this schema is
    the graph-facing representation while the dataclass remains the internal
    Python object used by node implementations and service persistence.
    """

    run_id: str
    project_id: str
    user_query: str
    task_type: str | None
    entities: dict[str, Any]
    retrieval_plan: dict[str, Any]
    evidence: list[dict]
    timeline: list[dict]
    log_findings: dict[str, Any]
    deploy_findings: dict[str, Any]
    previous_incidents: list[dict]
    hypotheses: list[dict]
    selected_root_cause: dict[str, Any]
    risk_level: str | None
    pending_approval: bool
    incident_report_draft: dict[str, Any]
    issue_draft: dict[str, Any]
    final_answer: dict[str, Any]
    status: str
    error: str | None


@dataclass
class InvestigationState:
    run_id: str
    project_id: str
    user_query: str
    task_type: str | None = None
    entities: dict[str, Any] = field(default_factory=dict)
    retrieval_plan: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict] = field(default_factory=list)
    timeline: list[dict] = field(default_factory=list)
    log_findings: dict[str, Any] = field(default_factory=dict)
    deploy_findings: dict[str, Any] = field(default_factory=dict)
    previous_incidents: list[dict] = field(default_factory=list)
    hypotheses: list[dict] = field(default_factory=list)
    selected_root_cause: dict[str, Any] = field(default_factory=dict)
    risk_level: str | None = None
    pending_approval: bool = False
    incident_report_draft: dict[str, Any] = field(default_factory=dict)
    issue_draft: dict[str, Any] = field(default_factory=dict)
    final_answer: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"
    error: str | None = None


def state_to_graph_dict(state: InvestigationState) -> InvestigationGraphState:
    """Convert the dataclass state into the dictionary state LangGraph expects."""

    return InvestigationGraphState(**asdict(state))


def state_from_graph_dict(state: InvestigationGraphState | dict[str, Any]) -> InvestigationState:
    """Convert LangGraph dictionary state back into the internal dataclass."""

    return InvestigationState(
        run_id=str(state["run_id"]),
        project_id=str(state["project_id"]),
        user_query=str(state["user_query"]),
        task_type=state.get("task_type"),
        entities=dict(state.get("entities") or {}),
        retrieval_plan=dict(state.get("retrieval_plan") or {}),
        evidence=list(state.get("evidence") or []),
        timeline=list(state.get("timeline") or []),
        log_findings=dict(state.get("log_findings") or {}),
        deploy_findings=dict(state.get("deploy_findings") or {}),
        previous_incidents=list(state.get("previous_incidents") or []),
        hypotheses=list(state.get("hypotheses") or []),
        selected_root_cause=dict(state.get("selected_root_cause") or {}),
        risk_level=state.get("risk_level"),
        pending_approval=bool(state.get("pending_approval", False)),
        incident_report_draft=dict(state.get("incident_report_draft") or {}),
        issue_draft=dict(state.get("issue_draft") or {}),
        final_answer=dict(state.get("final_answer") or {}),
        status=str(state.get("status") or "queued"),
        error=state.get("error"),
    )
