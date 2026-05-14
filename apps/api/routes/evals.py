from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_db
from incidentops.db.models import EvalRun
from incidentops.eval.runner import run_eval_persisted
from incidentops.schemas.api import EvalRunRequest, EvalRunResponse

router = APIRouter(prefix="/v1/evals", tags=["Evals"])


@router.get("")
async def list_evals(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(EvalRun).order_by(EvalRun.created_at.desc()))
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
async def run_eval_endpoint(body: EvalRunRequest, db: AsyncSession = Depends(get_db)):
    run = await run_eval_persisted(db, body.project_id, top_k=body.top_k, cases_path=body.cases_path)
    return EvalRunResponse(
        eval_run_id=run.id,
        project_id=run.project_id,
        status=run.status.value,
        summary=run.summary_json or {},
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


@router.get("/{eval_run_id}", response_model=EvalRunResponse)
async def get_eval(eval_run_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(EvalRun).where(EvalRun.id == eval_run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Eval run not found")
    return EvalRunResponse(
        eval_run_id=run.id,
        project_id=run.project_id,
        status=run.status.value,
        summary=run.summary_json or {},
        created_at=run.created_at,
        updated_at=run.updated_at,
    )
