from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from incidentops.agent.service import execute_run
from incidentops.config.settings import Settings
from incidentops.db.models import AgentRun, EvalRun, EvalStatus, RunStatus
from incidentops.db.session import _get_session_factory
from incidentops.eval.runner import execute_eval_run
from incidentops.security.audit import record_audit_event
from incidentops.worker.schemas import Job

logger = logging.getLogger("incidentops.worker.jobs")

TERMINAL_RUN_STATUSES = {
    RunStatus.awaiting_approval,
    RunStatus.completed,
    RunStatus.failed,
    RunStatus.rejected,
}
TERMINAL_EVAL_STATUSES = {EvalStatus.completed, EvalStatus.failed}


async def execute_job(job: Job, settings: Settings) -> None:
    if job.job_type == "execute_workflow_run":
        await execute_workflow_run_job(job.payload, settings)
        return
    if job.job_type == "execute_eval_run":
        await execute_eval_run_job(job.payload, settings)
        return
    raise ValueError(f"Unsupported job type: {job.job_type}")


async def execute_workflow_run_job(payload: dict, settings: Settings) -> None:
    run_id = uuid.UUID(str(payload["run_id"]))
    top_k = int(payload.get("top_k") or settings.default_top_k)
    reranker_model = str(payload.get("reranker_model") or settings.reranker_model)
    create_issue_draft = bool(payload.get("create_issue_draft", True))

    factory = _get_session_factory()
    async with factory() as db:
        result = await db.execute(select(AgentRun).where(AgentRun.id == run_id))
        run = result.scalar_one_or_none()
        if not run:
            logger.warning("workflow job references missing run_id=%s", run_id)
            return
        if run.status in TERMINAL_RUN_STATUSES:
            logger.info("workflow run_id=%s already terminal status=%s", run_id, run.status.value)
            return
        await record_audit_event(
            db,
            action="workflow_job_started",
            status="running",
            project_id=run.project_id,
            user_id=run.user_id,
            resource_type="agent_run",
            resource_id=run.id,
        )
        run = await execute_run(
            db,
            run,
            top_k,
            reranker_model,
            create_issue_draft=create_issue_draft,
            settings=settings,
        )
        await record_audit_event(
            db,
            action="workflow_job_failed" if run.status == RunStatus.failed else "workflow_job_completed",
            status=run.status.value,
            project_id=run.project_id,
            user_id=run.user_id,
            resource_type="agent_run",
            resource_id=run.id,
            metadata={"top_k": top_k},
        )
        await db.commit()


async def execute_eval_run_job(payload: dict, settings: Settings) -> None:
    eval_run_id = uuid.UUID(str(payload["eval_run_id"]))
    top_k = int(payload.get("top_k") or settings.default_top_k)
    cases_path = payload.get("cases_path")

    factory = _get_session_factory()
    async with factory() as db:
        result = await db.execute(select(EvalRun).where(EvalRun.id == eval_run_id))
        run = result.scalar_one_or_none()
        if not run:
            logger.warning("eval job references missing eval_run_id=%s", eval_run_id)
            return
        if run.status in TERMINAL_EVAL_STATUSES:
            logger.info("eval_run_id=%s already terminal status=%s", eval_run_id, run.status.value)
            return
        await record_audit_event(
            db,
            action="eval_job_started",
            status="running",
            project_id=run.project_id,
            resource_type="eval_run",
            resource_id=run.id,
        )
        run = await execute_eval_run(db, run, top_k=top_k, cases_path=cases_path)
        await record_audit_event(
            db,
            action="eval_job_failed" if run.status == EvalStatus.failed else "eval_job_completed",
            status=run.status.value,
            project_id=run.project_id,
            resource_type="eval_run",
            resource_id=run.id,
            metadata={"top_k": top_k},
        )
        await db.commit()

