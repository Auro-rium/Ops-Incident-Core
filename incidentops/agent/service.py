from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.agent.events import append_run_event
from incidentops.agent.graph import graph_engine_name, run_agent_graph
from incidentops.agent.state import InvestigationState
from incidentops.agent.tools import search_evidence
from incidentops.config.settings import Settings, get_settings
from incidentops.db.models import (
    AgentRun,
    Approval,
    ApprovalStatus,
    DraftStatus,
    IncidentReportDraft,
    IssueDraft,
    RunStatus,
)
from incidentops.observability.metrics import incr, observe_latency
from incidentops.observability.tracing import traced

logger = logging.getLogger("incidentops.agent.service")

NON_RETRYABLE_NODES = {"wait_for_human_approval"}


async def create_run(db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID | None, query: str) -> AgentRun:
    run = AgentRun(project_id=project_id, user_id=user_id, user_query=query, status=RunStatus.queued)
    db.add(run)
    await db.flush()
    await append_run_event(db, run.id, "run_created", payload={"query": query})
    return run


async def execute_run(
    db: AsyncSession,
    run: AgentRun,
    top_k: int,
    reranker_model: str,
    create_issue_draft: bool = True,
    settings: Settings | None = None,
) -> AgentRun:
    settings = settings or get_settings()
    started = time.time()
    state = InvestigationState(run_id=str(run.id), project_id=str(run.project_id), user_query=run.user_query, status="running")
    run.status = RunStatus.running
    run.error = None
    await append_run_event(db, run.id, "run_started", payload={"status": "running"})
    incr("workflow_runs_total")

    try:
        async with asyncio.timeout(settings.workflow_run_timeout_seconds):
            with traced("workflow.retrieve_evidence"):
                evidence = await search_evidence(
                    db,
                    run.project_id,
                    run.user_query,
                    top_k=top_k,
                    reranker_model=reranker_model,
                )
            state.evidence = evidence
            await append_run_event(db, run.id, "retrieved_evidence", payload={"count": len(evidence)})

            await append_run_event(
                db,
                run.id,
                "workflow_graph_started",
                payload={"engine": graph_engine_name()},
            )

            async def node_runner(node_name, node, node_state):
                return await execute_node_with_observability(
                    db, run.id, node_name, node, node_state, settings
                )

            state = await run_agent_graph(state, node_runner=node_runner, prefer_langgraph=True)

            await append_run_event(
                db,
                run.id,
                "workflow_graph_completed",
                payload={"engine": graph_engine_name(), "status": state.status},
            )

            run.task_type = state.task_type
            run.risk_level = state.risk_level
            run.state_json = jsonable_encoder(state.__dict__)
            run.final_answer_json = jsonable_encoder(state.final_answer)
            if state.pending_approval and create_issue_draft:
                run.status = RunStatus.awaiting_approval
                run.pending_approval = True
                await _ensure_pending_approval(db, run)
            else:
                run.status = RunStatus.completed
                run.pending_approval = False
            await _persist_drafts(db, run.id, state)
            await append_run_event(db, run.id, "run_finished", payload={"status": run.status.value})
    except Exception as exc:
        error = _safe_error(exc)
        logger.exception("workflow run failed run_id=%s error=%s", run.id, error)
        state.status = "failed"
        state.error = error
        run.status = RunStatus.failed
        run.error = error
        run.pending_approval = False
        run.state_json = jsonable_encoder(state.__dict__)
        await append_run_event(db, run.id, "run_failed", payload={"error": error})
        incr("workflow_failures_total")
    finally:
        runtime_ms = int((time.time() - started) * 1000)
        observe_latency("workflow_run_duration", runtime_ms)
        await db.flush()
        await db.refresh(run)
    return run


async def execute_node_with_observability(
    db: AsyncSession,
    run_id,
    node_name: str,
    node,
    state: InvestigationState,
    settings: Settings,
) -> InvestigationState:
    attempts_allowed = 1 if node_name in NON_RETRYABLE_NODES else max(1, int(settings.workflow_max_retries) + 1)
    attempt = 0
    last_error: Exception | None = None
    while attempt < attempts_allowed:
        attempt += 1
        started = time.time()
        await append_run_event(db, run_id, "node_started", node_name=node_name, payload={"attempt": attempt})
        try:
            with traced(f"workflow.node.{node_name}"):
                new_state = await asyncio.wait_for(
                    _call_node(node, state),
                    timeout=settings.workflow_node_timeout_seconds,
                )
            latency_ms = int((time.time() - started) * 1000)
            observe_latency(f"workflow_node_{node_name}", latency_ms)
            await append_run_event(
                db,
                run_id,
                "node_completed",
                node_name=node_name,
                payload={
                    "attempt": attempt,
                    "latency_ms": latency_ms,
                    "status": new_state.status,
                    "pending_approval": new_state.pending_approval,
                },
            )
            return new_state
        except Exception as exc:
            last_error = exc
            latency_ms = int((time.time() - started) * 1000)
            error = _safe_error(exc)
            observe_latency(f"workflow_node_{node_name}", latency_ms)
            incr("workflow_node_failures_total")
            await append_run_event(
                db,
                run_id,
                "node_failed",
                node_name=node_name,
                payload={"attempt": attempt, "latency_ms": latency_ms, "error": error},
            )
            if attempt < attempts_allowed:
                await append_run_event(
                    db,
                    run_id,
                    "node_retried",
                    node_name=node_name,
                    payload={"attempt": attempt + 1},
                )
                continue
            break
    raise RuntimeError(f"Node {node_name} failed: {_safe_error(last_error or RuntimeError('unknown error'))}")


async def _call_node(node, state: InvestigationState) -> InvestigationState:
    if not inspect.iscoroutinefunction(node):
        return await asyncio.to_thread(node, state)
    result = node(state)
    if inspect.isawaitable(result):
        return await result
    return result


async def _ensure_pending_approval(db: AsyncSession, run: AgentRun) -> None:
    result = await db.execute(
        select(Approval).where(
            Approval.run_id == run.id,
            Approval.action_type == "create_github_issue",
            Approval.status == ApprovalStatus.pending,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        return
    approval = Approval(
        run_id=run.id,
        action_type="create_github_issue",
        requested_by_user_id=run.user_id,
        status=ApprovalStatus.pending,
    )
    db.add(approval)
    await db.flush()


async def _persist_drafts(db: AsyncSession, run_id, state: InvestigationState) -> None:
    if state.incident_report_draft:
        existing_report = await db.execute(
            select(IncidentReportDraft).where(IncidentReportDraft.run_id == run_id)
        )
        if not existing_report.scalar_one_or_none():
            db.add(
                IncidentReportDraft(
                    run_id=run_id,
                    content_markdown=state.incident_report_draft.get("markdown", ""),
                    content_json=state.incident_report_draft,
                    status=DraftStatus.draft,
                )
            )
    if state.issue_draft:
        existing_issue = await db.execute(select(IssueDraft).where(IssueDraft.run_id == run_id))
        if not existing_issue.scalar_one_or_none():
            db.add(
                IssueDraft(
                    run_id=run_id,
                    title=state.issue_draft.get("title", "incident follow-up"),
                    body_markdown=state.issue_draft.get("body", ""),
                    provider="github",
                    status=DraftStatus.draft,
                )
            )
        await db.flush()


async def resume_after_approval(db: AsyncSession, run: AgentRun, approved: bool, rationale: str | None) -> AgentRun:
    result = await db.execute(
        select(Approval).where(Approval.run_id == run.id).order_by(Approval.created_at.desc())
    )
    approval = result.scalars().first()
    if not approval:
        return run
    approval.status = ApprovalStatus.approved if approved else ApprovalStatus.rejected
    approval.rationale = rationale
    run.pending_approval = False
    run.status = RunStatus.completed if approved else RunStatus.rejected
    final_answer = run.final_answer_json or {}
    final_answer["approval"] = {"status": approval.status.value, "rationale": rationale}
    run.final_answer_json = final_answer
    await append_run_event(
        db,
        run.id,
        "approval_resolved",
        payload={"status": approval.status.value, "rationale": rationale},
    )
    return run


def _safe_error(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:1000]
