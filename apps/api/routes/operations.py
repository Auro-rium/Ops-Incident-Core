"""Project-scoped operational-agent visibility and bounded job submission."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import ensure_project_access, get_db, get_settings_dep, require_user
from incidentops.config.settings import Settings
from incidentops.db.models import OperationalFinding, OperationalRun, OperationalRunStatus, ProjectRole
from incidentops.operations.service import (
    RUN_LOGGING,
    RUN_OBSERVER,
    create_operational_run,
    run_logging_aggregate,
    run_observer,
)
from incidentops.schemas.api import OperationalFindingResponse, OperationalRunRequest, OperationalRunResponse
from incidentops.security.audit import record_audit_event
from incidentops.worker.queue import get_job_queue

router = APIRouter(prefix="/v1/projects/{project_id}/operations", tags=["Operations"])


@router.get("/runs", response_model=list[OperationalRunResponse])
async def list_operational_runs(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    await ensure_project_access(db, project_id, user, settings, minimum_role=ProjectRole.viewer)
    runs = list((await db.execute(select(OperationalRun).where(OperationalRun.project_id == project_id).order_by(OperationalRun.created_at.desc()).limit(100))).scalars())
    return [_run_response(run) for run in runs]


@router.get("/findings", response_model=list[OperationalFindingResponse])
async def list_operational_findings(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    await ensure_project_access(db, project_id, user, settings, minimum_role=ProjectRole.viewer)
    findings = list((await db.execute(select(OperationalFinding).where(OperationalFinding.project_id == project_id).order_by(OperationalFinding.created_at.desc()).limit(200))).scalars())
    return [_finding_response(finding) for finding in findings]


@router.post("/observer/runs", response_model=OperationalRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def enqueue_observer(
    project_id: uuid.UUID,
    body: OperationalRunRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    return await _enqueue(project_id, RUN_OBSERVER, "execute_observer_agent", body, request, db, settings, user)


@router.post("/logging/runs", response_model=OperationalRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def enqueue_logging_aggregate(
    project_id: uuid.UUID,
    body: OperationalRunRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    return await _enqueue(project_id, RUN_LOGGING, "aggregate_operational_events", body, request, db, settings, user)


async def _enqueue(project_id, run_type, job_type, body, request, db, settings, user):
    await ensure_project_access(db, project_id, user, settings, minimum_role=ProjectRole.admin)
    run = await create_operational_run(db, project_id, run_type, request={}, idempotency_key=body.idempotency_key)
    if run.status.value == "queued":
        await record_audit_event(
            db,
            action=f"{run_type}_job_enqueued",
            status="queued",
            project_id=project_id,
            user=user,
            resource_type="operational_run",
            resource_id=run.id,
            request=request,
            metadata={"run_type": run_type, "queue_backend": settings.job_queue_backend},
        )
        if settings.worker_mode != "queue":
            if run_type == RUN_OBSERVER:
                await run_observer(db, run, settings)
            else:
                await run_logging_aggregate(db, run, settings)
            await db.commit()
        else:
            await db.commit()
            try:
                await get_job_queue(settings).enqueue(job_type, {"operational_run_id": str(run.id)})
            except Exception as exc:
                run.status = OperationalRunStatus.failed
                run.error_code = exc.__class__.__name__.lower()[:64]
                run.summary_json = {"status": "failed", "error_code": run.error_code}
                await db.commit()
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Failed to enqueue operational job") from exc
    await db.refresh(run)
    return _run_response(run)


def _run_response(run: OperationalRun) -> OperationalRunResponse:
    return OperationalRunResponse(
        operational_run_id=run.id,
        project_id=run.project_id,
        run_type=run.run_type,
        status=run.status.value,
        summary=run.summary_json or {},
        attempts=run.attempts,
        model_call_count=run.model_call_count,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        error_code=run.error_code,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _finding_response(finding: OperationalFinding) -> OperationalFindingResponse:
    return OperationalFindingResponse(
        finding_id=finding.id,
        operational_run_id=finding.operational_run_id,
        project_id=finding.project_id,
        finding_type=finding.finding_type,
        severity=finding.severity,
        status=finding.status,
        evidence=finding.evidence_json or {},
        threshold=finding.threshold_json or {},
        recommended_action=finding.recommended_action,
        created_at=finding.created_at,
    )
