from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from incidentops.config.settings import Settings
from incidentops.worker.schemas import Job

logger = logging.getLogger("incidentops.worker.queue")

DEFAULT_QUEUE_KEY = "incidentops:jobs"
FAILED_QUEUE_KEY = "incidentops:jobs:failed"
# Approximate (``MAXLEN ~``) caps so neither stream grows without bound.
DEFAULT_STREAM_MAXLEN = 100_000
FAILED_STREAM_MAXLEN = 10_000
FAILED_PAYLOAD_MAX_CHARS = 2000


class JobQueue(Protocol):
    async def enqueue(self, job_type: str, payload: dict) -> str:
        ...

    async def dequeue(self) -> Job | None:
        ...

    async def acknowledge(self, job_id: str) -> None:
        ...

    async def fail(self, job_id: str, error: str) -> None:
        ...

    async def retry(self, job: Job, *, delay_seconds: float) -> None:
        ...


def _retry_copy(job: Job, delay_seconds: float) -> Job:
    return job.model_copy(
        update={
            "attempts": job.attempts + 1,
            "not_before": datetime.now(timezone.utc) + timedelta(seconds=max(0.0, delay_seconds)),
        }
    )


def _truncated_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, default=str)[:FAILED_PAYLOAD_MAX_CHARS]


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
        job = self._jobs.popleft()
        if not job.is_ready():
            self._jobs.append(job)
            return None
        return job

    async def acknowledge(self, job_id: str) -> None:
        return None

    async def fail(self, job_id: str, error: str) -> None:
        self.failed[job_id] = error[:1000]

    async def retry(self, job: Job, *, delay_seconds: float) -> None:
        self._jobs.append(_retry_copy(job, delay_seconds))


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
        max_len: int = DEFAULT_STREAM_MAXLEN,
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
        self._max_len = max_len
        self._group_ready = False
        self._inflight: dict[str, tuple[str, Job]] = {}

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
        await self._client.xadd(
            self._queue_key,
            {"job": job.model_dump_json()},
            maxlen=self._max_len,
            approximate=True,
        )
        return job.id

    async def dequeue(self) -> Job | None:
        await self._ensure_group()
        entries = await self._reclaim_pending()
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
        if not entries:
            return None
        message_id, values = entries[0]
        raw = (values or {}).get("job")
        if not raw:
            # Redis 6.2 returns (id, None) for claimed entries that were deleted.
            await self._client.xack(self._queue_key, self._consumer_group, message_id)
            return None
        job = Job.model_validate_json(raw)
        if not job.is_ready():
            await self._republish(job, message_id)
            return None
        self._inflight[job.id] = (message_id, job)
        return job

    async def _reclaim_pending(self) -> list:
        try:
            response = await self._client.xautoclaim(
                self._queue_key,
                self._consumer_group,
                self._consumer_name,
                min_idle_time=self._claim_idle_ms,
                start_id="0-0",
                count=1,
            )
        except Exception:
            logger.error("Unable to reclaim pending Redis Stream jobs", exc_info=True)
            return []
        # Redis 6.2 returns [next_id, entries]; Redis 7 adds a third deleted-ids element.
        if not isinstance(response, (list, tuple)) or len(response) < 2:
            logger.error("Unexpected XAUTOCLAIM response shape: %r", type(response).__name__)
            return []
        return list(response[1] or [])

    async def _republish(self, job: Job, message_id: str) -> None:
        """Atomically XADD the job and XACK the delivery it replaces."""
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.xadd(
                self._queue_key,
                {"job": job.model_dump_json()},
                maxlen=self._max_len,
                approximate=True,
            )
            pipe.xack(self._queue_key, self._consumer_group, message_id)
            await pipe.execute()

    async def acknowledge(self, job_id: str) -> None:
        inflight = self._inflight.pop(job_id, None)
        if inflight:
            await self._client.xack(self._queue_key, self._consumer_group, inflight[0])

    async def retry(self, job: Job, *, delay_seconds: float) -> None:
        inflight = self._inflight.pop(job.id, None)
        retry_job = _retry_copy(job, delay_seconds)
        if inflight is None:
            await self._ensure_group()
            await self._client.xadd(
                self._queue_key,
                {"job": retry_job.model_dump_json()},
                maxlen=self._max_len,
                approximate=True,
            )
            return
        await self._republish(retry_job, inflight[0])

    async def fail(self, job_id: str, error: str) -> None:
        inflight = self._inflight.pop(job_id, None)
        fields = {"job_id": job_id, "error": error[:1000]}
        if inflight:
            job = inflight[1]
            fields.update(
                {
                    "job_type": job.job_type,
                    "attempts": str(job.attempts),
                    "payload": _truncated_payload(job.payload),
                }
            )
        await self._client.xadd(FAILED_QUEUE_KEY, fields, maxlen=FAILED_STREAM_MAXLEN, approximate=True)
        if inflight:
            await self._client.xack(self._queue_key, self._consumer_group, inflight[0])


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
