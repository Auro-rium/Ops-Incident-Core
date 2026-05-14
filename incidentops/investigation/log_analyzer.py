from __future__ import annotations

import re

LATENCY_RE = re.compile(r"(\d{2,6})ms")
ERROR_RE = re.compile(r"\b(ERROR|CRITICAL|EXCEPTION|5\d\d)\b", re.IGNORECASE)
TIMEOUT_RE = re.compile(r"\bTIMEOUT|timed out\b", re.IGNORECASE)


def analyze_logs(evidence: list[dict]) -> dict:
    findings = {
        "slow_requests": 0,
        "errors": 0,
        "timeouts": 0,
        "max_latency_ms": 0,
        "services": set(),
        "timestamped_logs": 0,
        "summary_points": [],
    }
    for item in evidence:
        if item.get("source_type") != "logs":
            continue
        text = item.get("text", "")
        if item.get("service_name"):
            findings["services"].add(item["service_name"])
        if item.get("timestamp_start"):
            findings["timestamped_logs"] += 1
        latencies = [int(match) for match in LATENCY_RE.findall(text)]
        findings["slow_requests"] += sum(1 for latency in latencies if latency >= 500)
        findings["max_latency_ms"] = max(findings["max_latency_ms"], max(latencies, default=0))
        findings["errors"] += len(ERROR_RE.findall(text))
        findings["timeouts"] += len(TIMEOUT_RE.findall(text))
    if findings["slow_requests"]:
        findings["summary_points"].append("logs contain slow request evidence")
    if findings["errors"]:
        findings["summary_points"].append("logs contain elevated error markers")
    if findings["timeouts"]:
        findings["summary_points"].append("logs contain timeout evidence")
    findings["services"] = sorted(findings["services"])
    return findings
