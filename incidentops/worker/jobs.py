from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from incidentops.agent.service import execute_run
from incidentops.config.settings import Settings
from incidentops.db.models import AgentRun, EvalRun, EvalStatus, IndexJob, RunStatus, Source, SourceSync
from incidentops.db.session import _get_session_factory
from incidentops.eval.runner import execute_eval_run
from incidentops.ingestion.indexer import index_normalized_documents
from incidentops.ingestion.normalized import NormalizedDocument
from incidentops.retrieval.embeddings import embed_texts_async
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
    if job.job_type == "index_document":
        await execute_index_document_job(job.payload, settings)
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


async def execute_index_document_job(payload: dict, settings: Settings) -> None:
    """Parse, embed, and publish a redacted pending document by durable ID."""
    index_job_id = uuid.UUID(str(payload["index_job_id"]))
    factory = _get_session_factory()
    async with factory() as db:
        result = await db.execute(
            select(IndexJob).where(IndexJob.id == index_job_id).with_for_update(skip_locked=True)
        )
        index_job = result.scalar_one_or_none()
        if not index_job:
            logger.info("index job unavailable or already locked id=%s", index_job_id)
            return
        if index_job.status in {"completed", "stale", "running"}:
            return
        if index_job.attempts >= settings.rag_index_max_retries:
            index_job.status = "failed"
            index_job.error_code = index_job.error_code or "retry_limit_exceeded"
            index_job.finished_at = datetime.now(timezone.utc)
            await db.commit()
            return
        if index_job.index_version != settings.rag_index_version:
            index_job.status = "stale"
            index_job.error_code = "index_version_changed"
            index_job.finished_at = datetime.now(timezone.utc)
            await db.commit()
            return

        index_job.status = "running"
        index_job.attempts += 1
        index_job.started_at = datetime.now(timezone.utc)
        try:
            normalized = NormalizedDocument.model_validate(index_job.normalized_payload_json)

            async def embed_fn(texts: list[str]) -> list[list[float]]:
                return await embed_texts_async(texts, model_name=settings.embedding_model)

            indexed = await index_normalized_documents(
                db,
                index_job.project_id,
                index_job.source_id,
                index_job.sync_id,
                [normalized],
                embed_fn=embed_fn,
            )
            index_job.status = "completed" if not indexed.errors else "failed"
            index_job.error_code = indexed.errors[0].code if indexed.errors else None
            index_job.finished_at = datetime.now(timezone.utc)

            if index_job.sync_id:
                sync_result = await db.execute(select(SourceSync).where(SourceSync.id == index_job.sync_id))
                sync = sync_result.scalar_one_or_none()
                if sync:
                    sync.chunks_created += indexed.chunks_created
                    sync.parser_errors += len(indexed.errors)
                    diagnostics = dict(sync.diagnostics_json or {})
                    diagnostics["index_jobs_completed"] = int(diagnostics.get("index_jobs_completed", 0) or 0) + 1
                    diagnostics["index_jobs_failed"] = int(diagnostics.get("index_jobs_failed", 0) or 0) + bool(indexed.errors)
                    sync.diagnostics_json = diagnostics
                    if sync.finished_at is not None:
                        pending_result = await db.execute(
                            select(IndexJob.id).where(
                                IndexJob.sync_id == sync.id,
                                IndexJob.status.in_(("pending", "queued", "running")),
                            )
                        )
                        if pending_result.first() is None:
                            failed_result = await db.execute(
                                select(IndexJob.id).where(IndexJob.sync_id == sync.id, IndexJob.status == "failed")
                            )
                            source_result = await db.execute(select(Source).where(Source.id == index_job.source_id))
                            source = source_result.scalar_one_or_none()
                            if source:
                                source.status = "error" if failed_result.first() is not None else "ready"
                                source.last_sync_status = "partial_success" if source.status == "error" else "success"

            await record_audit_event(
                db,
                action="index_job_completed" if index_job.status == "completed" else "index_job_failed",
                status=index_job.status,
                project_id=index_job.project_id,
                resource_type="index_job",
                resource_id=index_job.id,
                metadata={"index_version": index_job.index_version, "chunks_created": indexed.chunks_created},
            )
            await db.commit()
        except Exception as exc:
            logger.exception("index job failed index_job_id=%s", index_job_id)
            retryable = index_job.attempts < settings.rag_index_max_retries
            index_job.status = "pending" if retryable else "failed"
            index_job.error_code = exc.__class__.__name__.lower()[:64]
            index_job.finished_at = None if retryable else datetime.now(timezone.utc)
            await record_audit_event(
                db,
                action="index_job_retry_scheduled" if retryable else "index_job_failed",
                status=index_job.status,
                project_id=index_job.project_id,
                resource_type="index_job",
                resource_id=index_job.id,
                metadata={"index_version": index_job.index_version, "error_code": index_job.error_code},
            )
            await db.commit()
            raise
