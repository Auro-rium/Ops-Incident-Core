from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

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
from incidentops.agent.state import (
    InvestigationGraphState,
    InvestigationState,
    state_from_graph_dict,
    state_to_graph_dict,
)

try:  # LangGraph is optional in local/test installs unless the runtime extra is installed.
    from langgraph.graph import END, START, StateGraph
except ModuleNotFoundError:  # pragma: no cover - exercised when langgraph is absent.
    END = START = StateGraph = None  # type: ignore[assignment]
    LANGGRAPH_AVAILABLE = False
else:
    LANGGRAPH_AVAILABLE = True

AgentNode = Callable[[InvestigationState], InvestigationState]
NodeRunner = Callable[[str, AgentNode, InvestigationState], Awaitable[InvestigationState]]

NODE_ORDER: list[tuple[str, AgentNode]] = [
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


def build_agent_graph(node_runner: NodeRunner | None = None):
    """Build and compile the LangGraph investigation workflow.

    The graph is intentionally deterministic: LangGraph owns the orchestration
    and state passing, while the existing Python node functions keep the domain
    logic unchanged. ``node_runner`` lets the service wrap each node with
    persistence, metrics, timeout, retry, and run-event logging.
    """

    if not LANGGRAPH_AVAILABLE:
        raise RuntimeError("langgraph is not installed")

    graph = StateGraph(InvestigationGraphState)

    for node_name, node in NODE_ORDER:
        graph.add_node(node_name, _make_graph_node(node_name, node, node_runner))

    graph.add_edge(START, NODE_ORDER[0][0])
    for (current_name, _), (next_name, _) in zip(NODE_ORDER, NODE_ORDER[1:], strict=False):
        graph.add_edge(current_name, next_name)
    graph.add_edge(NODE_ORDER[-1][0], END)

    return graph.compile()


async def run_agent_graph(
    initial_state: InvestigationState,
    node_runner: NodeRunner | None = None,
    *,
    prefer_langgraph: bool = True,
) -> InvestigationState:
    """Execute the investigation workflow through LangGraph when available.

    The sequential fallback exists only so local/test environments without the
    optional LangGraph dependency do not break. In production, install the
    LangGraph extra and this function will execute the compiled StateGraph.
    """

    if prefer_langgraph and LANGGRAPH_AVAILABLE:
        compiled = build_agent_graph(node_runner=node_runner)
        result = await compiled.ainvoke(state_to_graph_dict(initial_state))
        return state_from_graph_dict(result)

    state = initial_state
    for node_name, node in NODE_ORDER:
        if node_runner is not None:
            state = await node_runner(node_name, node, state)
        else:
            state = await _call_node_direct(node, state)
    return state


def graph_engine_name() -> str:
    return "langgraph" if LANGGRAPH_AVAILABLE else "sequential_fallback"


def _make_graph_node(node_name: str, node: AgentNode, node_runner: NodeRunner | None):
    async def graph_node(graph_state: InvestigationGraphState) -> InvestigationGraphState:
        state = state_from_graph_dict(graph_state)
        if node_runner is not None:
            state = await node_runner(node_name, node, state)
        else:
            state = await _call_node_direct(node, state)
        return state_to_graph_dict(state)

    graph_node.__name__ = f"{node_name}_graph_node"
    return graph_node


async def _call_node_direct(node: AgentNode, state: InvestigationState) -> InvestigationState:
    result: Any = node(state)
    if inspect.isawaitable(result):
        return await result
    return result
