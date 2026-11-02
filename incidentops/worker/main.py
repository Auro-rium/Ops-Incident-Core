from __future__ import annotations

import argparse
import asyncio
import logging
import uuid

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from incidentops.config.settings import get_settings
from incidentops.config.validation import validate_startup_settings
from incidentops.observability.metrics import incr, observe_latency
from incidentops.observability.tracing import traced
from incidentops.operations.service import mark_operational_run_failed
from incidentops.worker.jobs import TERMINAL_RUN_STATUSES, execute_job
from incidentops.worker.index_dispatch import dispatch_recoverable_index_jobs
from incidentops.worker.queue import get_job_queue

logger = logging.getLogger("incidentops.worker")

RETRY_BASE_DELAY_SECONDS = 5
RETRY_MAX_DELAY_SECONDS = 300
# The run enforces its own timeout; the outer guard only catches a run stuck past it.
WORKFLOW_TIMEOUT_MARGIN_SECONDS = 30
RETRYABLE_JOB_TYPES = {"execute_evaluator_agent", "execute_observer_agent", "aggregate_operational_events"}


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
        jobs = []
        for _ in range(max(1, settings.worker_concurrency)):
            job = await queue.dequeue()
            if job is None:
                break
            jobs.append(job)
        if not jobs:
            if once:
                return
            await asyncio.sleep(settings.job_poll_interval_seconds)
            continue
        await asyncio.gather(*(_process_job(job, queue, settings) for job in jobs))
        if once:
            return


async def _process_job(job, queue, settings) -> None:
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
    except Exception as exc:
        error = _safe_error(exc)
        incr("jobs_failed_total")
        logger.exception("job failed id=%s type=%s attempts=%s error=%s", job.id, job.job_type, job.attempts, error)
        if job.job_type == "index_document":
            # The Postgres index_jobs dispatcher is the sole retry owner for indexing.
            await queue.acknowledge(job.id)
        elif _can_retry(job, exc, settings):
            delay = _retry_delay_seconds(job.attempts)
            await queue.retry(job, delay_seconds=delay)
            incr("jobs_retried_total")
            logger.warning("job retry scheduled id=%s type=%s delay_s=%s error=%s", job.id, job.job_type, delay, error)
        else:
            if job.job_type == "execute_workflow_run":
                await _mark_workflow_failure(job.payload, error)
            await _mark_operational_failure(job.payload, error)
            await queue.fail(job.id, error)
    finally:
        observe_latency("job_duration", (asyncio.get_running_loop().time() - start) * 1000)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s — %(message)s")
    args = build_parser().parse_args()
    asyncio.run(run_worker(once=args.once))


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "job_timeout"
    return exc.__class__.__name__.lower()[:64]


def _can_retry(job, exc: Exception, settings) -> bool:
    if job.attempts >= settings.worker_job_max_retries:
        return False
    if job.job_type == "execute_workflow_run":
        return _is_transient(exc)
    return job.job_type in RETRYABLE_JOB_TYPES


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return False
    if isinstance(exc, (ConnectionError, RedisConnectionError, RedisTimeoutError, OperationalError, InterfaceError)):
        return True
    if isinstance(exc, DBAPIError):
        return bool(exc.connection_invalidated)
    return isinstance(exc, OSError)


def _retry_delay_seconds(attempts: int) -> int:
    return min(RETRY_BASE_DELAY_SECONDS * (2 ** max(0, attempts)), RETRY_MAX_DELAY_SECONDS)


def _job_timeout_seconds(job_type: str, settings) -> int:
    if job_type == "execute_workflow_run":
        return settings.workflow_run_timeout_seconds + WORKFLOW_TIMEOUT_MARGIN_SECONDS
    if job_type in {"execute_eval_run", "execute_evaluator_agent"}:
        return settings.eval_run_timeout_seconds
    if job_type in {"execute_observer_agent", "aggregate_operational_events"}:
        return settings.operational_agent_timeout_seconds
    if job_type == "index_document":
        return settings.index_job_timeout_seconds
    return max(settings.rag_remote_timeout_seconds * 4, 30)


async def _mark_workflow_failure(payload: dict, error: str) -> None:
    raw_run_id = payload.get("run_id")
    if not raw_run_id:
        return
    from incidentops.agent.events import append_run_event
    from incidentops.db.models import AgentRun, RunStatus
    from incidentops.db.session import _get_session_factory

    try:
        async with _get_session_factory()() as db:
            run = (
                await db.execute(select(AgentRun).where(AgentRun.id == uuid.UUID(str(raw_run_id))).with_for_update())
            ).scalar_one_or_none()
            if not run or run.status in TERMINAL_RUN_STATUSES:
                return
            run.status = RunStatus.failed
            run.error = error
            run.pending_approval = False
            await append_run_event(db, run.id, "run_failed", payload={"error": error})
            await db.commit()
    except Exception:
        logger.exception("failed to persist workflow run failure")


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
