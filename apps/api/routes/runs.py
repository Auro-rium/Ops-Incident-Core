from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_db, get_settings_dep, require_user
from incidentops.agent.service import create_run, execute_run, resume_after_approval
from incidentops.config.settings import Settings
from incidentops.db.models import AgentRun, AgentRunEvent, ProjectRole
from incidentops.schemas.api import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    RunCreateRequest,
    RunEventResponse,
    RunResponse,
)
from incidentops.security.rbac import require_project_role

router = APIRouter(prefix="/v1", tags=["Runs"])


@router.post("/runs", response_model=RunResponse)
async def start_run(
    body: RunCreateRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    await require_project_role(db, body.project_id, user.id, ProjectRole.investigator)
    run = await create_run(db, body.project_id, user.id, body.query)
    run = await execute_run(db, run, body.top_k, settings.reranker_model, create_issue_draft=body.create_issue_draft)
    return RunResponse(
        run_id=run.id,
        project_id=run.project_id,
        status=run.status.value,
        task_type=run.task_type,
        risk_level=run.risk_level,
        pending_approval=run.pending_approval,
        final_answer=run.final_answer_json,
        error=run.error,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AgentRun).where(AgentRun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunResponse(
        run_id=run.id,
        project_id=run.project_id,
        status=run.status.value,
        task_type=run.task_type,
        risk_level=run.risk_level,
        pending_approval=run.pending_approval,
        final_answer=run.final_answer_json,
        error=run.error,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


@router.get("/runs/{run_id}/events")
async def get_run_events(run_id: uuid.UUID, stream: bool = False, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(AgentRunEvent).where(AgentRunEvent.run_id == run_id).order_by(AgentRunEvent.sequence_no.asc())
    )
    events = result.scalars().all()
    if not stream:
        return [
            RunEventResponse(
                sequence_no=event.sequence_no,
                event_type=event.event_type,
                node_name=event.node_name,
                payload=event.payload_json or {},
                created_at=event.created_at,
            )
            for event in events
        ]

    async def event_stream():
        for event in events:
            payload = RunEventResponse(
                sequence_no=event.sequence_no,
                event_type=event.event_type,
                node_name=event.node_name,
                payload=event.payload_json or {},
                created_at=event.created_at,
            )
            yield f"data: {payload.model_dump_json()}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/runs/{run_id}/approve", response_model=ApprovalDecisionResponse)
async def approve_run(
    run_id: uuid.UUID,
    body: ApprovalDecisionRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_user),
):
    result = await db.execute(select(AgentRun).where(AgentRun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    await require_project_role(db, run.project_id, user.id, ProjectRole.approver)
    await resume_after_approval(db, run, approved=True, rationale=body.rationale)
    return ApprovalDecisionResponse(run_id=run.id, status=run.status.value, rationale=body.rationale)


@router.post("/runs/{run_id}/reject", response_model=ApprovalDecisionResponse)
async def reject_run(
    run_id: uuid.UUID,
    body: ApprovalDecisionRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_user),
):
    result = await db.execute(select(AgentRun).where(AgentRun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    await require_project_role(db, run.project_id, user.id, ProjectRole.approver)
    await resume_after_approval(db, run, approved=False, rationale=body.rationale)
    return ApprovalDecisionResponse(run_id=run.id, status=run.status.value, rationale=body.rationale)
