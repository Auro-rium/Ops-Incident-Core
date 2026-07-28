from __future__ import annotations

import argparse
import asyncio
import logging
import uuid

from incidentops.config.settings import get_settings
from incidentops.config.validation import validate_startup_settings
from incidentops.observability.metrics import incr, observe_latency
from incidentops.observability.tracing import traced
from incidentops.operations.service import mark_operational_run_failed
from incidentops.worker.jobs import execute_job
from incidentops.worker.index_dispatch import dispatch_recoverable_index_jobs
from incidentops.worker.queue import get_job_queue

logger = logging.getLogger("incidentops.worker")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the IncidentOps Core worker.")
    parser.add_argument("--once", action="store_true", help="Process one available job and exit.")
    return parser


async def run_worker(*, once: bool = False) -> None:
    settings = get_settings()
    validate_startup_settings(settings)
    queue = get_job_queue(settings)
    logger.info("worker started mode=%s queue_backend=%s", settings.worker_mode, settings.job_queue_backend)
    while True:
        await dispatch_recoverable_index_jobs(queue, settings)
        job = await queue.dequeue()
        if job is None:
            if once:
                return
            await asyncio.sleep(settings.job_poll_interval_seconds)
            continue
        start = asyncio.get_running_loop().time()
        try:
            incr("jobs_started_total")
            logger.info("job started id=%s type=%s", job.id, job.job_type)
            with traced(f"worker.job.{job.job_type}"):
                await asyncio.wait_for(
                    execute_job(job, settings),
                    timeout=_job_timeout_seconds(job.job_type, settings),
                )
            await queue.acknowledge(job.id)
            incr("jobs_completed_total")
            logger.info("job completed id=%s type=%s", job.id, job.job_type)
        except Exception as exc:  # pragma: no cover - worker-level guard
            error = _safe_error(exc)
            incr("jobs_failed_total")
            if _can_retry(job.job_type, job.payload, settings):
                retry_payload = dict(job.payload)
                retry_payload["_worker_attempt"] = int(job.payload.get("_worker_attempt", 0) or 0) + 1
                await queue.enqueue(job.job_type, retry_payload)
                await queue.acknowledge(job.id)
                incr("jobs_retried_total")
                logger.warning("job retry scheduled id=%s type=%s error=%s", job.id, job.job_type, error)
            else:
                await _mark_operational_failure(job.payload, error)
                await queue.fail(job.id, error)
            logger.exception("job failed id=%s type=%s error=%s", job.id, job.job_type, error)
        finally:
            observe_latency("job_duration", (asyncio.get_running_loop().time() - start) * 1000)
        if once:
            return


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s — %(message)s")
    args = build_parser().parse_args()
    asyncio.run(run_worker(once=args.once))


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "job_timeout"
    return exc.__class__.__name__.lower()[:64]


def _can_retry(job_type: str, payload: dict, settings) -> bool:
    if job_type not in {"index_document", "execute_evaluator_agent", "execute_observer_agent", "aggregate_operational_events"}:
        return False
    return int(payload.get("_worker_attempt", 0) or 0) < settings.worker_job_max_retries


def _job_timeout_seconds(job_type: str, settings) -> int:
    if job_type == "execute_workflow_run":
        return settings.workflow_run_timeout_seconds
    if job_type in {"execute_eval_run", "execute_evaluator_agent"}:
        return settings.eval_run_timeout_seconds
    if job_type in {"execute_observer_agent", "aggregate_operational_events"}:
        return settings.operational_agent_timeout_seconds
    return max(settings.rag_remote_timeout_seconds * 4, 30)


async def _mark_operational_failure(payload: dict, error: str) -> None:
    raw_run_id = payload.get("operational_run_id")
    if not raw_run_id:
        return
    from incidentops.db.session import _get_session_factory

    try:
        async with _get_session_factory()() as db:
            await mark_operational_run_failed(db, uuid.UUID(str(raw_run_id)), error)
    except Exception:
        logger.exception("failed to persist operational run failure")


if __name__ == "__main__":
    main()
