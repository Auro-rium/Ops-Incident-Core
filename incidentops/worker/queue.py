from __future__ import annotations

import logging
from collections import deque
from typing import Protocol

from incidentops.config.settings import Settings
from incidentops.worker.schemas import Job

logger = logging.getLogger("incidentops.worker.queue")

DEFAULT_QUEUE_KEY = "incidentops:jobs"
FAILED_QUEUE_KEY = "incidentops:jobs:failed"


class JobQueue(Protocol):
    async def enqueue(self, job_type: str, payload: dict) -> str:
        ...

    async def dequeue(self) -> Job | None:
        ...

    async def acknowledge(self, job_id: str) -> None:
        ...

    async def fail(self, job_id: str, error: str) -> None:
        ...


class InlineQueue:
    """In-process FIFO queue for local tests and development workers."""

    def __init__(self) -> None:
        self._jobs: deque[Job] = deque()
        self.failed: dict[str, str] = {}

    async def enqueue(self, job_type: str, payload: dict) -> str:
        job = Job(job_type=job_type, payload=payload)
        self._jobs.append(job)
        return job.id

    async def dequeue(self) -> Job | None:
        if not self._jobs:
            return None
        return self._jobs.popleft()

    async def acknowledge(self, job_id: str) -> None:
        return None

    async def fail(self, job_id: str, error: str) -> None:
        self.failed[job_id] = error[:1000]


class RedisQueue:
    """Small Redis list-backed queue.

    This intentionally avoids Celery-style orchestration. The durable state for
    workflow/eval execution remains in Postgres; Redis only transports jobs.
    """

    def __init__(self, redis_url: str, queue_key: str = DEFAULT_QUEUE_KEY) -> None:
        try:
            import redis.asyncio as redis
        except ImportError as exc:  # pragma: no cover - deployment dependency guard
            raise RuntimeError("JOB_QUEUE_BACKEND=redis requires the redis package") from exc
        self._client = redis.from_url(redis_url, decode_responses=True)
        self._queue_key = queue_key

    async def enqueue(self, job_type: str, payload: dict) -> str:
        job = Job(job_type=job_type, payload=payload)
        await self._client.lpush(self._queue_key, job.model_dump_json())
        return job.id

    async def dequeue(self) -> Job | None:
        item = await self._client.brpop(self._queue_key, timeout=1)
        if not item:
            return None
        _, raw = item
        return Job.model_validate_json(raw)

    async def acknowledge(self, job_id: str) -> None:
        return None

    async def fail(self, job_id: str, error: str) -> None:
        await self._client.lpush(FAILED_QUEUE_KEY, f"{job_id}:{error[:1000]}")


_inline_queue = InlineQueue()
_redis_queue: RedisQueue | None = None


def get_job_queue(settings: Settings) -> JobQueue:
    global _redis_queue
    if settings.job_queue_backend == "redis":
        if _redis_queue is None:
            _redis_queue = RedisQueue(settings.resolved_redis_url)
        return _redis_queue
    return _inline_queue
