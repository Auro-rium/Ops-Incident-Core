from __future__ import annotations

import re

from incidentops.investigation.schemas import InvestigationEntities
from incidentops.retrieval.hybrid_search import analyze_query

SYMPTOM_PATTERNS = {
    "latency": ("latency", "slow", "p95", "p99", "slower"),
    "timeout": ("timeout", "timed out", "504"),
    "error_rate": ("5xx", "500", "502", "503", "error rate", "errors"),
    "crash": ("crash", "segfault", "panic"),
    "memory": ("memory", "oom", "heap"),
    "database": ("database", "db", "postgres", "mysql", "sql"),
    "auth": ("auth", "login", "token", "permission"),
    "payment": ("payment", "charge", "billing"),
    "queue": ("queue", "kafka", "rabbit", "consumer", "lag"),
}
TIME_WINDOW_RE = re.compile(r"\b(last\s+\d+\s+(?:m|minutes|h|hours|d|days)|between\s+.+?)\b", re.IGNORECASE)


def extract_entities(query: str) -> InvestigationEntities:
    analyzed = analyze_query(query)
    lower = query.lower()
    symptom = None
    for label, patterns in SYMPTOM_PATTERNS.items():
        if any(pattern in lower for pattern in patterns):
            symptom = label
            break
    time_window = None
    if match := TIME_WINDOW_RE.search(query):
        time_window = {"raw": match.group(1)}
    service_name = analyzed["services"][0] if analyzed["services"] else None
    endpoint = analyzed["endpoints"][0] if analyzed["endpoints"] else None
    deploy_hash = analyzed["deploy_hashes"][0] if analyzed["deploy_hashes"] else None
    return InvestigationEntities(
        service_name=service_name,
        endpoint=endpoint,
        deploy_hash=deploy_hash,
        symptom=symptom,
        time_window=time_window,
        raw_matches=analyzed,
    )
