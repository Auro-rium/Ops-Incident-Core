from __future__ import annotations

from incidentops.agent.nodes import (
    analyze_deploy_diff_node,
    analyze_logs_node,
    classify_task_node,
    draft_incident_report_node,
    draft_issue_node,
    extract_entities_node,
    finalize_answer_node,
    find_previous_incidents_node,
    generate_hypotheses_node,
    resolve_scope_node,
    risk_gate_node,
    select_root_cause_node,
    wait_for_human_approval_node,
)

NODE_ORDER = [
    ("classify_task", classify_task_node),
    ("extract_entities", extract_entities_node),
    ("resolve_scope", resolve_scope_node),
    ("analyze_logs", analyze_logs_node),
    ("analyze_deploy_diff", analyze_deploy_diff_node),
    ("find_previous_incidents", find_previous_incidents_node),
    ("generate_hypotheses", generate_hypotheses_node),
    ("select_root_cause", select_root_cause_node),
    ("draft_incident_report", draft_incident_report_node),
    ("draft_issue", draft_issue_node),
    ("risk_gate", risk_gate_node),
    ("wait_for_human_approval", wait_for_human_approval_node),
    ("finalize_answer", finalize_answer_node),
]
