from __future__ import annotations

import argparse
import asyncio
import logging

from incidentops.config.settings import get_settings
from incidentops.config.validation import validate_startup_settings
from incidentops.observability.metrics import incr, observe_latency
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
            await execute_job(job, settings)
            await queue.acknowledge(job.id)
            incr("jobs_completed_total")
            logger.info("job completed id=%s type=%s", job.id, job.job_type)
        except Exception as exc:  # pragma: no cover - worker-level guard
            error = _safe_error(exc)
            incr("jobs_failed_total")
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
    return str(exc).replace("\n", " ")[:1000]


if __name__ == "__main__":
    main()
