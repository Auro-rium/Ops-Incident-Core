from __future__ import annotations


def classify_task(query: str) -> str:
    lower = query.lower()
    if any(token in lower for token in ("latency", "slow", "slower", "spike", "p95", "p99")):
        return "latency_investigation"
    if any(token in lower for token in ("5xx", "error rate", "error spike", "500", "502", "503", "504")):
        return "error_rate_investigation"
    if "deploy" in lower or "release" in lower or "regression" in lower:
        return "deploy_regression"
    if "timeout" in lower or "timed out" in lower:
        return "timeout_investigation"
    if "previous incident" in lower or "happened before" in lower or "similar incident" in lower:
        return "previous_incident_lookup"
    return "generic_incident_question"
