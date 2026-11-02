from __future__ import annotations

from incidentops.agent.tools import (
    draft_github_issue,
    draft_incident_report,
    find_similar_incidents,
    get_deploy_diff,
    get_logs_for_window,
    request_approval,
)
from incidentops.investigation.classifier import classify_task
from incidentops.investigation.deploy_analyzer import analyze_deploy_diff
from incidentops.investigation.entity_extractor import extract_entities
from incidentops.investigation.hypothesis_generator import generate_hypotheses
from incidentops.investigation.incident_matcher import find_previous_incidents
from incidentops.investigation.log_analyzer import analyze_logs
from incidentops.investigation.root_cause_selector import select_root_cause
from incidentops.investigation.schemas import InvestigationEntities
from incidentops.investigation.scope_resolver import resolve_scope
from incidentops.investigation.timeline_builder import build_timeline


def classify_task_node(state):
    state.task_type = classify_task(state.user_query)
    return state


def extract_entities_node(state):
    entities = extract_entities(state.user_query)
    state.entities = {
        "service_name": entities.service_name,
        "endpoint": entities.endpoint,
        "deploy_hash": entities.deploy_hash,
        "symptom": entities.symptom,
        "time_window": entities.time_window,
    }
    return state


RISK_LEVELS = ("low", "medium", "high")
CONFIDENCE_RISK_POINTS = {"high": 0, "medium": 1, "low": 2}
APPROVAL_MIN_RISK = "medium"
_ENTITY_FIELDS = ("service_name", "endpoint", "deploy_hash", "symptom", "time_window")


def resolve_scope_node(state):
    if state.entities:
        entities = InvestigationEntities(**{key: state.entities.get(key) for key in _ENTITY_FIELDS})
    else:
        entities = extract_entities(state.user_query)
    state.retrieval_plan = resolve_scope(entities)
    return state


def analyze_logs_node(state):
    state.log_findings = analyze_logs(get_logs_for_window(state.evidence))
    return state


def analyze_deploy_diff_node(state):
    state.deploy_findings = analyze_deploy_diff(
        get_deploy_diff(state.evidence, state.entities.get("deploy_hash")),
        state.entities.get("deploy_hash"),
    )
    return state


def find_previous_incidents_node(state):
    state.previous_incidents = find_previous_incidents(
        find_similar_incidents(state.evidence),
        state.entities,
    )
    return state


def generate_hypotheses_node(state):
    state.hypotheses = [
        hypothesis.__dict__
        for hypothesis in generate_hypotheses(
            state.entities,
            state.evidence,
            state.log_findings,
            state.deploy_findings,
            state.previous_incidents,
        )
    ]
    return state


def select_root_cause_node(state):
    from incidentops.investigation.schemas import Hypothesis

    hypotheses = [Hypothesis(**hypothesis) for hypothesis in state.hypotheses]
    state.selected_root_cause = select_root_cause(hypotheses, state.evidence).__dict__
    return state


def compose_answer_node(state):
    """Build the answer content (incl. suggested fix) before any drafts use it."""
    timeline = build_timeline(
        state.evidence,
        state.log_findings,
        state.deploy_findings,
        state.previous_incidents,
    )
    suggested_fix = None
    if state.selected_root_cause.get("confidence") != "low":
        suggested_fix = "Validate the leading hypothesis against deploy history, logs, and ownership documentation before taking action."
    state.timeline = [
        {
            "ts": event.ts.isoformat() if event.ts else None,
            "event_type": event.event_type,
            "summary": event.summary,
            "evidence_chunk_ids": event.evidence_chunk_ids,
        }
        for event in timeline
    ]
    state.final_answer = {
        "question": state.user_query,
        "task_type": state.task_type,
        "entities": state.entities,
        "timeline": state.timeline,
        "hypotheses": state.hypotheses,
        "likely_root_cause": state.selected_root_cause,
        "affected_services": sorted(
            {
                service
                for service in [
                    state.entities.get("service_name"),
                    *state.log_findings.get("services", []),
                ]
                if service
            }
        ),
        "suggested_fix": suggested_fix,
        "citations": [item.get("citation", {}) for item in state.evidence],
        "evidence": state.evidence,
        "unknowns": [] if state.previous_incidents else ["No prior incident match reached confidence threshold."],
    }
    return state


def draft_incident_report_node(state):
    state.incident_report_draft = draft_incident_report(
        {
            "task_type": state.task_type,
            "likely_root_cause": state.selected_root_cause,
            "suggested_fix": state.final_answer.get("suggested_fix") or "",
        }
    )
    return state


def draft_issue_node(state):
    if not state.create_issue_draft:
        state.issue_draft = {}
        return state
    state.issue_draft = draft_github_issue(
        {
            "likely_root_cause": state.selected_root_cause,
            "suggested_fix": state.final_answer.get("suggested_fix") or "",
        }
    )
    return state


def missing_data_count(state) -> int:
    return sum(
        [
            not state.deploy_findings.get("diff_count"),
            not state.log_findings.get("timestamped_logs"),
            not state.previous_incidents,
            not any(item.get("source_type") in {"runbook", "api_doc"} for item in state.evidence),
        ]
    )


def compute_risk_level(confidence: str | None, missing_count: int, has_issue_draft: bool) -> str:
    points = CONFIDENCE_RISK_POINTS.get(str(confidence or "low"), CONFIDENCE_RISK_POINTS["low"])
    points += 0 if missing_count <= 0 else 1 if missing_count <= 2 else 2
    points += 1 if has_issue_draft else 0
    if points <= 1:
        return "low"
    return "medium" if points <= 3 else "high"


def risk_gate_node(state):
    has_issue_draft = bool(state.issue_draft)
    state.risk_level = compute_risk_level(
        state.selected_root_cause.get("confidence"),
        missing_data_count(state),
        has_issue_draft,
    )
    risky = RISK_LEVELS.index(state.risk_level) >= RISK_LEVELS.index(APPROVAL_MIN_RISK)
    state.pending_approval = bool(
        has_issue_draft and risky and request_approval("create_github_issue")["requires_approval"]
    )
    return state


def wait_for_human_approval_node(state):
    state.status = "awaiting_approval" if state.pending_approval else state.status
    return state


def finalize_answer_node(state):
    if not state.final_answer:
        state = compose_answer_node(state)
    state.status = "completed" if not state.pending_approval else "awaiting_approval"
    return state
