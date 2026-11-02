from __future__ import annotations

import asyncio
import json
import time
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
from incidentops.agent.service import ApprovalConflictError, create_run, execute_run, resume_after_approval
from incidentops.config.settings import Settings
from incidentops.db.models import AgentRun, AgentRunEvent, ProjectRole, RunStatus
from incidentops.db.session import _get_session_factory
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

EVENT_STREAM_POLL_INTERVAL_SECONDS = 1.0
EVENT_STREAM_MAX_SECONDS = 300.0
# awaiting_approval ends the stream too: nothing more happens until a human decides.
EVENT_STREAM_END_STATUSES = {RunStatus.awaiting_approval, RunStatus.completed, RunStatus.failed, RunStatus.rejected}


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
    if stream:
        # FastAPI keeps this session open until the stream ends; give its connection back.
        await db.commit()
        return StreamingResponse(
            _stream_run_events(
                run_id,
                _get_session_factory(),
                poll_interval=EVENT_STREAM_POLL_INTERVAL_SECONDS,
                max_seconds=EVENT_STREAM_MAX_SECONDS,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    result = await db.execute(
        select(AgentRunEvent).where(AgentRunEvent.run_id == run_id).order_by(AgentRunEvent.sequence_no.asc())
    )
    return [_event_response(event) for event in result.scalars().all()]


async def _stream_run_events(run_id: uuid.UUID, session_factory, *, poll_interval: float, max_seconds: float):
    """Yield new run events as SSE until the run stops progressing or the time cap passes.

    Each poll uses a short-lived session so an open stream does not pin a pooled
    connection. Status is read before events, so a poll that sees an ending
    status also delivers everything written before that status.
    """
    deadline = time.monotonic() + max_seconds
    last_sequence = 0
    while True:
        async with session_factory() as db:
            run_status = (await db.execute(select(AgentRun.status).where(AgentRun.id == run_id))).scalar_one_or_none()
            result = await db.execute(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == run_id, AgentRunEvent.sequence_no > last_sequence)
                .order_by(AgentRunEvent.sequence_no.asc())
            )
            events = result.scalars().all()
        for event in events:
            last_sequence = event.sequence_no
            yield f"data: {_event_response(event).model_dump_json()}\n\n"
        finished = run_status is None or run_status in EVENT_STREAM_END_STATUSES
        if finished or time.monotonic() >= deadline:
            end = {"status": run_status.value if run_status else None, "reason": "finished" if finished else "time_cap"}
            yield f"event: end\ndata: {json.dumps(end)}\n\n"
            return
        await asyncio.sleep(poll_interval)


def _event_response(event: AgentRunEvent) -> RunEventResponse:
    return RunEventResponse(
        sequence_no=event.sequence_no,
        event_type=event.event_type,
        node_name=event.node_name,
        payload=event.payload_json or {},
        created_at=event.created_at,
    )


@router.post("/runs/{run_id}/approve", response_model=ApprovalDecisionResponse)
async def approve_run(
    run_id: uuid.UUID,
    body: ApprovalDecisionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    return await _decide_run(run_id, body, request, db, settings, user, approved=True)


@router.post("/runs/{run_id}/reject", response_model=ApprovalDecisionResponse)
async def reject_run(
    run_id: uuid.UUID,
    body: ApprovalDecisionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    return await _decide_run(run_id, body, request, db, settings, user, approved=False)


async def _decide_run(run_id, body, request, db, settings, user, *, approved: bool) -> ApprovalDecisionResponse:
    result = await db.execute(select(AgentRun).where(AgentRun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    await ensure_project_access(db, run.project_id, user, settings, minimum_role=ProjectRole.approver)
    # ensure_project_access returns early on a demo bypass; approval always needs the role.
    await require_project_role(db, run.project_id, user.id, ProjectRole.approver)
    try:
        run = await resume_after_approval(
            db, run, approved=approved, rationale=body.rationale, resolved_by_user_id=user.id
        )
    except ApprovalConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await record_audit_event(
        db,
        action="approval_approved" if approved else "approval_rejected",
        status="success",
        project_id=run.project_id,
        user=user,
        resource_type="agent_run",
        resource_id=run.id,
        request=request,
    )
    return ApprovalDecisionResponse(run_id=run.id, status=run.status.value, rationale=body.rationale)


def _safe_error(exc: Exception) -> str:
    return exc.__class__.__name__.lower()[:64]
