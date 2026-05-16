from __future__ import annotations

import time
from collections import defaultdict
from contextlib import contextmanager
from re import sub

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
    return {
        "counters": dict(COUNTERS),
        "latencies_ms": latencies,
        "latency_counts": dict(LATENCY_COUNTS),
    }


def prometheus_text() -> str:
    lines = [
        "# HELP incidentops_counter_total IncidentOps in-process counters.",
        "# TYPE incidentops_counter_total counter",
    ]
    counter_values: dict[str, float] = defaultdict(float)
    for name, value in sorted(COUNTERS.items()):
        metric_name = _metric_name(name)
        suffix = "" if metric_name.endswith("_total") else "_total"
        counter_values[f"incidentops_{metric_name}{suffix}"] += float(value)
    for metric_name, value in sorted(counter_values.items()):
        lines.append(f"{metric_name} {value}")
    lines.extend(
        [
            "# HELP incidentops_latency_average_ms IncidentOps average observed latency in milliseconds.",
            "# TYPE incidentops_latency_average_ms gauge",
        ]
    )
    for name, total in sorted(LATENCY_TOTALS.items()):
        count = LATENCY_COUNTS[name] or 1
        metric_name = _metric_name(name)
        lines.append(f"incidentops_{metric_name}_latency_average_ms {float(total) / count}")
        lines.append(f"incidentops_{metric_name}_latency_count {int(LATENCY_COUNTS[name])}")
    return "\n".join(lines) + "\n"


def _metric_name(name: str) -> str:
    cleaned = sub(r"[^a-zA-Z0-9_]+", "_", name.strip().lower())
    return cleaned.strip("_") or "unnamed"
