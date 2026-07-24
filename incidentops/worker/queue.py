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
    """Redis Streams queue with consumer groups and pending-entry recovery.

    Postgres remains the durable business-state authority. Redis Streams adds
    acknowledgement and recovery semantics missing from the old list queue.
    """

    def __init__(
        self,
        redis_url: str,
        queue_key: str = DEFAULT_QUEUE_KEY,
        *,
        consumer_group: str = "incidentops-workers",
        consumer_name: str = "core-worker",
        claim_idle_ms: int = 60000,
    ) -> None:
        try:
            import redis.asyncio as redis
        except ImportError as exc:  # pragma: no cover - deployment dependency guard
            raise RuntimeError("JOB_QUEUE_BACKEND=redis requires the redis package") from exc
        self._client = redis.from_url(redis_url, decode_responses=True)
        self._queue_key = queue_key
        self._consumer_group = consumer_group
        self._consumer_name = consumer_name
        self._claim_idle_ms = claim_idle_ms
        self._group_ready = False
        self._inflight: dict[str, str] = {}

    async def _ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            await self._client.xgroup_create(self._queue_key, self._consumer_group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    async def enqueue(self, job_type: str, payload: dict) -> str:
        job = Job(job_type=job_type, payload=payload)
        await self._ensure_group()
        await self._client.xadd(self._queue_key, {"job": job.model_dump_json()})
        return job.id

    async def dequeue(self) -> Job | None:
        await self._ensure_group()
        try:
            _, claimed, _ = await self._client.xautoclaim(
                self._queue_key,
                self._consumer_group,
                self._consumer_name,
                min_idle_time=self._claim_idle_ms,
                start_id="0-0",
                count=1,
            )
            entries = claimed
        except Exception:
            logger.warning("Unable to reclaim pending Redis Stream jobs", exc_info=True)
            entries = []
        if not entries:
            response = await self._client.xreadgroup(
                self._consumer_group,
                self._consumer_name,
                {self._queue_key: ">"},
                count=1,
                block=1000,
            )
            if not response:
                return None
            _, entries = response[0]
        message_id, values = entries[0]
        raw = values.get("job")
        if not raw:
            await self._client.xack(self._queue_key, self._consumer_group, message_id)
            return None
        job = Job.model_validate_json(raw)
        self._inflight[job.id] = message_id
        return job

    async def acknowledge(self, job_id: str) -> None:
        message_id = self._inflight.pop(job_id, None)
        if message_id:
            await self._client.xack(self._queue_key, self._consumer_group, message_id)

    async def fail(self, job_id: str, error: str) -> None:
        message_id = self._inflight.pop(job_id, None)
        await self._client.xadd(FAILED_QUEUE_KEY, {"job_id": job_id, "error": error[:1000]})
        if message_id:
            await self._client.xack(self._queue_key, self._consumer_group, message_id)


_inline_queue = InlineQueue()
_redis_queue: RedisQueue | None = None


def get_job_queue(settings: Settings) -> JobQueue:
    global _redis_queue
    if settings.job_queue_backend == "redis":
        if _redis_queue is None:
            _redis_queue = RedisQueue(
                settings.resolved_redis_url,
                consumer_group=settings.job_queue_consumer_group,
                consumer_name=settings.job_queue_consumer_name,
                claim_idle_ms=settings.job_queue_claim_idle_ms,
            )
        return _redis_queue
    return _inline_queue
