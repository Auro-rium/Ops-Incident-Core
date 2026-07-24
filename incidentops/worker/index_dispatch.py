"""Postgres-backed dispatch and recovery for asynchronous index jobs.

Redis Streams carries work to workers, but Postgres is the source of truth.
This dispatcher makes jobs recoverable after an API process dies between its
database commit and queue publication, without putting document content in
Redis payloads.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update

from incidentops.config.settings import Settings
from incidentops.db.models import IndexJob
from incidentops.db.session import _get_session_factory
from incidentops.worker.queue import JobQueue

logger = logging.getLogger("incidentops.worker.index_dispatch")


async def dispatch_recoverable_index_jobs(queue: JobQueue, settings: Settings) -> int:
    """Publish pending or abandoned queue records using only their durable IDs."""
    if not settings.rag_async_indexing:
        return 0

    now = datetime.now(timezone.utc)
    reclaim_before = now - timedelta(milliseconds=settings.job_queue_claim_idle_ms)
    factory = _get_session_factory()
    async with factory() as db:
        result = await db.execute(
            select(IndexJob)
            .where(
                IndexJob.attempts < settings.rag_index_max_retries,
                or_(
                    IndexJob.status == "pending",
                    (IndexJob.status == "queued") & (IndexJob.queued_at < reclaim_before),
                ),
            )
            .order_by(IndexJob.created_at.asc())
            .limit(settings.rag_index_dispatch_batch_size)
            .with_for_update(skip_locked=True)
        )
        jobs = list(result.scalars())
        if not jobs:
            return 0
        for job in jobs:
            job.status = "queued"
            job.queued_at = now
        await db.commit()

    published = 0
    for job in jobs:
        try:
            await queue.enqueue("index_document", {"index_job_id": str(job.id)})
            published += 1
        except Exception:
            logger.exception("failed to publish index job id=%s", job.id)
            async with factory() as db:
                await db.execute(
                    update(IndexJob)
                    .where(IndexJob.id == job.id, IndexJob.status == "queued")
                    .values(status="pending", queued_at=None)
                )
                await db.commit()
    if published:
        logger.info("dispatched recoverable index jobs count=%s", published)
    return published
