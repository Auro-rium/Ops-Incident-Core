from __future__ import annotations

import uuid

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.agent.events import append_run_event
from incidentops.agent.graph import NODE_ORDER
from incidentops.agent.state import InvestigationState
from incidentops.agent.tools import search_evidence
from incidentops.db.models import (
    AgentRun,
    Approval,
    ApprovalStatus,
    DraftStatus,
    IncidentReportDraft,
    IssueDraft,
    RunStatus,
)


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
) -> AgentRun:
    state = InvestigationState(run_id=str(run.id), project_id=str(run.project_id), user_query=run.user_query, status="running")
    run.status = RunStatus.running
    await append_run_event(db, run.id, "status", payload={"status": "running"})

    evidence = await search_evidence(db, run.project_id, run.user_query, top_k=top_k, reranker_model=reranker_model)
    state.evidence = evidence
    await append_run_event(db, run.id, "retrieved_evidence", payload={"count": len(evidence)})

    for node_name, node in NODE_ORDER:
        await append_run_event(db, run.id, "node_started", node_name=node_name, payload={})
        state = node(state)
        await append_run_event(
            db,
            run.id,
            "node_completed",
            node_name=node_name,
            payload={"status": state.status, "pending_approval": state.pending_approval},
        )
    run.task_type = state.task_type
    run.risk_level = state.risk_level
    run.state_json = jsonable_encoder(state.__dict__)
    run.final_answer_json = jsonable_encoder(state.final_answer)
    if state.pending_approval and create_issue_draft:
        run.status = RunStatus.awaiting_approval
        run.pending_approval = True
        approval = Approval(
            run_id=run.id,
            action_type="create_github_issue",
            requested_by_user_id=run.user_id,
            status=ApprovalStatus.pending,
        )
        db.add(approval)
        await db.flush()
    else:
        run.status = RunStatus.completed
    await _persist_drafts(db, run.id, state)
    await db.flush()
    await db.refresh(run)
    await append_run_event(db, run.id, "run_finished", payload={"status": run.status.value})
    return run


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


async def _persist_drafts(db: AsyncSession, run_id, state: InvestigationState) -> None:
    if state.incident_report_draft:
        db.add(
            IncidentReportDraft(
                run_id=run_id,
                content_markdown=state.incident_report_draft.get("markdown", ""),
                content_json=state.incident_report_draft,
                status=DraftStatus.draft,
            )
        )
    if state.issue_draft:
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
