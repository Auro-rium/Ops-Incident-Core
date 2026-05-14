from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
