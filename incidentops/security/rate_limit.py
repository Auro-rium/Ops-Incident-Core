from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Protocol

from fastapi import HTTPException, status

from incidentops.config.settings import Settings


class RateLimiter(Protocol):
    def check(self, key: str, limit: int, window_seconds: int) -> None:
        ...


class InMemoryRateLimiter:
    def __init__(self):
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_seconds: int) -> None:
        now = time.time()
        bucket = self._events[key]
        while bucket and bucket[0] <= now - window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")
        bucket.append(now)


class RedisRateLimiter:
    def __init__(self, redis_url: str):
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - depends on deployment extras
            raise RuntimeError("RATE_LIMIT_BACKEND=redis requires the redis package") from exc
        self._client = redis.Redis.from_url(redis_url, decode_responses=True)

    def check(self, key: str, limit: int, window_seconds: int) -> None:
        now = time.time()
        redis_key = f"incidentops:rate:{key}"
        pipe = self._client.pipeline()
        pipe.zremrangebyscore(redis_key, 0, now - window_seconds)
        pipe.zcard(redis_key)
        pipe.zadd(redis_key, {str(now): now})
        pipe.expire(redis_key, window_seconds)
        _, count, _, _ = pipe.execute()
        if int(count) >= limit:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")


_memory_limiter = InMemoryRateLimiter()
_redis_limiter: RedisRateLimiter | None = None


def get_rate_limiter(settings: Settings) -> RateLimiter:
    global _redis_limiter
    if settings.rate_limit_backend == "redis":
        if _redis_limiter is None:
            _redis_limiter = RedisRateLimiter(settings.resolved_redis_url)
        return _redis_limiter
    return _memory_limiter


limiter = _memory_limiter
