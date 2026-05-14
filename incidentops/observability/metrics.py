from __future__ import annotations

import time
from collections import defaultdict
from contextlib import contextmanager

COUNTERS: dict[str, float] = defaultdict(float)
LATENCY_TOTALS: dict[str, float] = defaultdict(float)
LATENCY_COUNTS: dict[str, int] = defaultdict(int)


def incr(name: str, value: float = 1.0) -> None:
    COUNTERS[name] += value


def observe_latency(name: str, ms: float) -> None:
    LATENCY_TOTALS[name] += ms
    LATENCY_COUNTS[name] += 1


@contextmanager
def timed(name: str):
    start = time.time()
    try:
        yield
    finally:
        observe_latency(name, (time.time() - start) * 1000)


def summary() -> dict:
    latencies = {}
    for key, total in LATENCY_TOTALS.items():
        count = LATENCY_COUNTS[key] or 1
        latencies[key] = round(total / count, 2)
    return {"counters": dict(COUNTERS), "latencies_ms": latencies}
