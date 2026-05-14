from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import check_rate_limit, ensure_project_access, get_db, get_settings_dep, require_user
from incidentops.config.settings import Settings
from incidentops.db.models import EvalRun, ProjectMember, ProjectRole
from incidentops.eval.runner import run_eval_persisted
from incidentops.schemas.api import EvalRunRequest, EvalRunResponse
from incidentops.security.audit import record_audit_event

router = APIRouter(prefix="/v1/evals", tags=["Evals"])


@router.get("")
async def list_evals(db: AsyncSession = Depends(get_db), user=Depends(require_user)):
    project_ids = select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
    result = await db.execute(
        select(EvalRun).where(EvalRun.project_id.in_(project_ids)).order_by(EvalRun.created_at.desc())
    )
    runs = result.scalars().all()
    return [
        EvalRunResponse(
            eval_run_id=run.id,
            project_id=run.project_id,
            status=run.status.value,
            summary=run.summary_json or {},
            created_at=run.created_at,
            updated_at=run.updated_at,
        )
        for run in runs
    ]


@router.post("/run", response_model=EvalRunResponse)
async def run_eval_endpoint(
    body: EvalRunRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    if body.top_k > settings.max_top_k or body.top_k > settings.max_retrieved_chunks:
        raise HTTPException(status_code=400, detail="top_k exceeds configured limit")
    await check_rate_limit(
        db,
        settings,
        f"eval:{user.id}",
        settings.user_request_limit,
        settings.rate_limit_window_seconds,
        user=user,
        project_id=body.project_id,
        action="eval_run",
    )
    await ensure_project_access(db, body.project_id, user, settings, minimum_role=ProjectRole.admin)
    run = await run_eval_persisted(db, body.project_id, top_k=body.top_k, cases_path=body.cases_path)
    await record_audit_event(
        db,
        action="eval_run_created",
        status="success",
        project_id=body.project_id,
        user=user,
        resource_type="eval_run",
        resource_id=run.id,
        request=request,
        metadata={"top_k": body.top_k, "cases_path": body.cases_path},
    )
    return EvalRunResponse(
        eval_run_id=run.id,
        project_id=run.project_id,
        status=run.status.value,
        summary=run.summary_json or {},
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


@router.get("/{eval_run_id}", response_model=EvalRunResponse)
async def get_eval(
    eval_run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    user=Depends(require_user),
):
    result = await db.execute(select(EvalRun).where(EvalRun.id == eval_run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Eval run not found")
    await ensure_project_access(db, run.project_id, user, settings, minimum_role=ProjectRole.viewer)
    return EvalRunResponse(
        eval_run_id=run.id,
        project_id=run.project_id,
        status=run.status.value,
        summary=run.summary_json or {},
        created_at=run.created_at,
        updated_at=run.updated_at,
    )
