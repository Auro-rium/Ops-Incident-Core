from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from incidentops.agent.service import execute_run
from incidentops.config.settings import Settings
from incidentops.db.models import AgentRun, EvalRun, EvalStatus, IndexJob, OperationalRun, OperationalRunStatus, RunStatus, Source, SourceSync
from incidentops.db.session import _get_session_factory
from incidentops.eval.runner import execute_eval_run
from incidentops.ingestion.indexer import index_normalized_documents
from incidentops.ingestion.normalized import NormalizedDocument
from incidentops.observability.metrics import incr, observe_latency
from incidentops.observability.tracing import traced
from incidentops.operations.service import (
    RUN_EVALUATOR,
    RUN_LOGGING,
    RUN_OBSERVER,
    record_operational_event,
    run_logging_aggregate,
    run_observer,
)
from incidentops.retrieval.embeddings import AsyncEmbeddingBatcher
from incidentops.security.audit import record_audit_event
from incidentops.worker.schemas import Job

logger = logging.getLogger("incidentops.worker.jobs")

_embedding_batcher: AsyncEmbeddingBatcher | None = None


def _get_embedding_batcher(settings: Settings) -> AsyncEmbeddingBatcher:
    global _embedding_batcher
    if _embedding_batcher is None:
        _embedding_batcher = AsyncEmbeddingBatcher(
            max_texts=settings.embedding_batch_max_texts,
            window_ms=settings.embedding_batch_window_ms,
        )
    return _embedding_batcher

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
    if job.job_type == "execute_evaluator_agent":
        await execute_evaluator_agent_job(job.payload, settings)
        return
    if job.job_type == "execute_observer_agent":
        await execute_operational_agent_job(job.payload, settings, RUN_OBSERVER)
        return
    if job.job_type == "aggregate_operational_events":
        await execute_operational_agent_job(job.payload, settings, RUN_LOGGING)
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


async def execute_evaluator_agent_job(payload: dict, settings: Settings) -> None:
    """Run persisted eval cases inside a durable operational-agent envelope."""
    eval_run_id = uuid.UUID(str(payload["eval_run_id"]))
    operational_run_id = uuid.UUID(str(payload["operational_run_id"]))
    top_k = int(payload.get("top_k") or settings.default_top_k)
    cases_path = payload.get("cases_path")
    factory = _get_session_factory()
    async with factory() as db:
        eval_run = (await db.execute(select(EvalRun).where(EvalRun.id == eval_run_id))).scalar_one_or_none()
        op_run = (await db.execute(select(OperationalRun).where(OperationalRun.id == operational_run_id))).scalar_one_or_none()
        if not eval_run or not op_run:
            logger.warning("evaluator job references missing run eval=%s operational=%s", eval_run_id, operational_run_id)
            return
        if op_run.run_type != RUN_EVALUATOR or op_run.status.value in {"completed", "failed"}:
            return
        op_run.status = OperationalRunStatus.running
        op_run.attempts += 1
        op_run.started_at = datetime.now(timezone.utc)
        await record_operational_event(
            db,
            project_id=op_run.project_id,
            operational_run_id=op_run.id,
            category="evaluator",
            event_type="evaluator_started",
            payload={"eval_run_id": str(eval_run.id), "top_k": top_k, "attempt": op_run.attempts},
        )
        await db.flush()
        started = datetime.now(timezone.utc)
        with traced("evaluator.run"):
            completed_eval = await execute_eval_run(
                db,
                eval_run,
                top_k=top_k,
                cases_path=cases_path,
                settings=settings,
            )
        summary = dict(completed_eval.summary_json or {})
        op_run.model_call_count = 0
        op_run.input_tokens = 0
        op_run.output_tokens = 0
        op_run.summary_json = {
            "status": "completed" if completed_eval.status != EvalStatus.failed else "failed",
            "eval_run_id": str(completed_eval.id),
            "total_cases": int(summary.get("total_cases") or 0),
            "failed_cases": int(summary.get("failed_cases") or 0),
            "avg_evidence_recall": float(summary.get("avg_evidence_recall") or summary.get("avg_recall") or 0.0),
            "wrong_source_type_rate": float(summary.get("wrong_source_type_rate") or 0.0),
            "latency_p95_ms": float(summary.get("latency_p95_ms") or 0.0),
            "model_judging_used": False,
        }
        op_run.status = OperationalRunStatus.completed if completed_eval.status != EvalStatus.failed else OperationalRunStatus.failed
        op_run.finished_at = datetime.now(timezone.utc)
        incr("operational_runs_total")
        incr("evaluator_runs_total")
        if op_run.status == OperationalRunStatus.failed:
            incr("operational_run_failures_total")
        observe_latency("evaluator_run", (op_run.finished_at - started).total_seconds() * 1000)
        await record_operational_event(
            db,
            project_id=op_run.project_id,
            operational_run_id=op_run.id,
            category="evaluator",
            event_type="evaluator_completed" if op_run.status.value == "completed" else "evaluator_failed",
            severity="info" if op_run.status.value == "completed" else "high",
            payload={"eval_run_id": str(completed_eval.id), "total_cases": summary.get("total_cases", 0), "failed_cases": summary.get("failed_cases", 0)},
        )
        await db.commit()


async def execute_operational_agent_job(payload: dict, settings: Settings, expected_type: str) -> None:
    operational_run_id = uuid.UUID(str(payload["operational_run_id"]))
    factory = _get_session_factory()
    async with factory() as db:
        run = (await db.execute(select(OperationalRun).where(OperationalRun.id == operational_run_id))).scalar_one_or_none()
        if not run:
            logger.warning("operational job references missing run_id=%s", operational_run_id)
            return
        if run.run_type != expected_type or run.status.value in {"completed", "failed"}:
            return
        if expected_type == RUN_OBSERVER:
            await run_observer(db, run, settings)
        elif expected_type == RUN_LOGGING:
            await run_logging_aggregate(db, run, settings)
        else:  # pragma: no cover - dispatch guard
            raise ValueError(f"unsupported operational agent type: {expected_type}")
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
        if index_job.index_version != settings.vector_index_version:
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
            batcher = _get_embedding_batcher(settings)

            async def embed_fn(texts: list[str]) -> list[list[float]]:
                return await batcher.embed(texts, model_name=settings.embedding_model, input_type="passage")

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
            await record_operational_event(
                db,
                project_id=index_job.project_id,
                category="indexing",
                event_type="index_document_completed" if index_job.status == "completed" else "index_document_failed",
                severity="info" if index_job.status == "completed" else "high",
                payload={
                    "index_job_id": str(index_job.id),
                    "index_version": index_job.index_version,
                    "chunks_created": indexed.chunks_created,
                    "error_code": index_job.error_code,
                },
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
            await record_operational_event(
                db,
                project_id=index_job.project_id,
                category="indexing",
                event_type="index_document_retry" if retryable else "index_document_failed",
                severity="medium" if retryable else "high",
                payload={"index_job_id": str(index_job.id), "index_version": index_job.index_version, "error_code": index_job.error_code},
            )
            await db.commit()
            raise
