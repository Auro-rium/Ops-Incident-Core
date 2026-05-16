from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import (
    check_rate_limit,
    enforce_query_limits,
    ensure_project_access,
    get_current_user,
    get_db,
    get_settings_dep,
    require_user,
)
from incidentops.agent.events import append_run_event
from incidentops.agent.service import create_run, execute_run, resume_after_approval
from incidentops.config.settings import Settings
from incidentops.db.models import AgentRun, AgentRunEvent, ProjectRole, RunStatus
from incidentops.observability.metrics import incr
from incidentops.schemas.api import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    RunCreateRequest,
    RunEventResponse,
    RunResponse,
)
from incidentops.security.rbac import require_project_role
from incidentops.security.audit import record_audit_event
from incidentops.worker.queue import get_job_queue

router = APIRouter(prefix="/v1", tags=["Runs"])


@router.post("/runs", response_model=RunResponse)
async def start_run(
    body: RunCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    enforce_query_limits(body.query, body.top_k, settings)
    await check_rate_limit(
        db,
        settings,
        f"run:{user.id}",
        settings.user_request_limit,
        settings.rate_limit_window_seconds,
        user=user,
        project_id=body.project_id,
        action="workflow_run",
    )
    await require_project_role(db, body.project_id, user.id, ProjectRole.investigator)
    run = await create_run(db, body.project_id, user.id, body.query)
    await record_audit_event(
        db,
        action="workflow_run_created",
        status="success",
        project_id=body.project_id,
        user=user,
        resource_type="agent_run",
        resource_id=run.id,
        request=request,
        metadata={"top_k": body.top_k, "create_issue_draft": body.create_issue_draft},
    )
    if settings.worker_mode == "queue":
        await append_run_event(
            db,
            run.id,
            "job_enqueued",
            payload={"job_type": "execute_workflow_run", "queue_backend": settings.job_queue_backend},
        )
        await record_audit_event(
            db,
            action="workflow_job_enqueued",
            status="queued",
            project_id=body.project_id,
            user=user,
            resource_type="agent_run",
            resource_id=run.id,
            request=request,
            metadata={"top_k": body.top_k, "queue_backend": settings.job_queue_backend},
        )
        await db.flush()
        await db.commit()
        try:
            await get_job_queue(settings).enqueue(
                "execute_workflow_run",
                {
                    "run_id": str(run.id),
                    "top_k": body.top_k,
                    "reranker_model": settings.reranker_model,
                    "create_issue_draft": body.create_issue_draft,
                },
            )
        except Exception as exc:
            error = _safe_error(exc)
            run.status = RunStatus.failed
            run.error = error
            await append_run_event(db, run.id, "run_failed", payload={"error": error})
            await record_audit_event(
                db,
                action="workflow_job_failed",
                status="failed",
                project_id=body.project_id,
                user=user,
                resource_type="agent_run",
                resource_id=run.id,
                request=request,
                metadata={"error": error},
            )
            await db.commit()
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Failed to enqueue workflow job") from exc
        incr("workflow_jobs_enqueued_total")
        await db.refresh(run)
    else:
        run = await execute_run(
            db,
            run,
            body.top_k,
            settings.reranker_model,
            create_issue_draft=body.create_issue_draft,
            settings=settings,
        )

    if run.pending_approval:
        await record_audit_event(
            db,
            action="approval_requested",
            status="pending",
            project_id=body.project_id,
            user=user,
            resource_type="agent_run",
            resource_id=run.id,
            request=request,
        )
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
async def get_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(get_current_user),
):
    result = await db.execute(select(AgentRun).where(AgentRun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    await ensure_project_access(db, run.project_id, user, settings, minimum_role=ProjectRole.viewer)
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
async def get_run_events(
    run_id: uuid.UUID,
    stream: bool = False,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(get_current_user),
):
    run_result = await db.execute(select(AgentRun).where(AgentRun.id == run_id))
    run = run_result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    await ensure_project_access(db, run.project_id, user, settings, minimum_role=ProjectRole.viewer)
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
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_user),
):
    result = await db.execute(select(AgentRun).where(AgentRun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    await require_project_role(db, run.project_id, user.id, ProjectRole.approver)
    await resume_after_approval(db, run, approved=True, rationale=body.rationale)
    await record_audit_event(
        db,
        action="approval_approved",
        status="success",
        project_id=run.project_id,
        user=user,
        resource_type="agent_run",
        resource_id=run.id,
        request=request,
    )
    return ApprovalDecisionResponse(run_id=run.id, status=run.status.value, rationale=body.rationale)


def _safe_error(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:1000]


@router.post("/runs/{run_id}/reject", response_model=ApprovalDecisionResponse)
async def reject_run(
    run_id: uuid.UUID,
    body: ApprovalDecisionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_user),
):
    result = await db.execute(select(AgentRun).where(AgentRun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    await require_project_role(db, run.project_id, user.id, ProjectRole.approver)
    await resume_after_approval(db, run, approved=False, rationale=body.rationale)
    await record_audit_event(
        db,
        action="approval_rejected",
        status="success",
        project_id=run.project_id,
        user=user,
        resource_type="agent_run",
        resource_id=run.id,
        request=request,
    )
    return ApprovalDecisionResponse(run_id=run.id, status=run.status.value, rationale=body.rationale)
