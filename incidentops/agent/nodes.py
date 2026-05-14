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


def resolve_scope_node(state):
    state.retrieval_plan = resolve_scope(extract_entities(state.user_query))
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


def draft_incident_report_node(state):
    state.incident_report_draft = draft_incident_report(
        {
            "task_type": state.task_type,
            "likely_root_cause": state.selected_root_cause,
            "suggested_fix": state.final_answer.get("suggested_fix", ""),
        }
    )
    return state


def draft_issue_node(state):
    state.issue_draft = draft_github_issue(
        {
            "likely_root_cause": state.selected_root_cause,
            "suggested_fix": state.final_answer.get("suggested_fix", ""),
        }
    )
    return state


def risk_gate_node(state):
    action = request_approval("create_github_issue")
    state.risk_level = "medium"
    state.pending_approval = action["requires_approval"]
    return state


def wait_for_human_approval_node(state):
    state.status = "awaiting_approval" if state.pending_approval else state.status
    return state


def finalize_answer_node(state):
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
    state.status = "completed" if not state.pending_approval else "awaiting_approval"
    return state
