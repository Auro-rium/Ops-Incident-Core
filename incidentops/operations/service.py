"""Durable, deterministic evaluator/observer/logging-agent primitives."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.config.settings import Settings
from incidentops.db.models import EvalRun, IndexJob, OperationalEvent, OperationalFinding, OperationalRun, OperationalRunStatus, SourceSync
from incidentops.observability.metrics import incr, observe_latency
from incidentops.security.audit import sanitize_audit_metadata
from incidentops.security.secret_redaction import redact_secrets

RUN_EVALUATOR = "evaluator"
RUN_OBSERVER = "observer"
RUN_LOGGING = "logging"
RUN_TYPES = {RUN_EVALUATOR, RUN_OBSERVER, RUN_LOGGING}
TERMINAL_STATUSES = {OperationalRunStatus.completed, OperationalRunStatus.failed}
_DROP_KEYS = {"content", "text", "raw", "document", "documents", "prompt", "evidence", "password", "secret", "token", "api_key", "authorization", "connection_string"}


def build_idempotency_key(run_type: str, request: dict[str, Any] | None = None) -> str:
    value = json.dumps({"run_type": run_type, "request": request or {}}, sort_keys=True, default=str)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:48]


async def create_operational_run(db: AsyncSession, project_id: uuid.UUID, run_type: str, *, request: dict[str, Any] | None = None, idempotency_key: str | None = None) -> OperationalRun:
    if run_type not in RUN_TYPES:
        raise ValueError(f"unsupported operational run type: {run_type}")
    key = (idempotency_key or build_idempotency_key(run_type, request)).strip()[:128]
    existing = (await db.execute(select(OperationalRun).where(OperationalRun.project_id == project_id, OperationalRun.run_type == run_type, OperationalRun.idempotency_key == key))).scalar_one_or_none()
    if existing:
        return existing
    run = OperationalRun(project_id=project_id, run_type=run_type, idempotency_key=key, request_json=sanitize_operational_payload(request or {}), summary_json={"status": "queued"})
    db.add(run)
    await db.flush()
    return run


async def record_operational_event(db: AsyncSession, *, project_id: uuid.UUID, category: str, event_type: str, severity: str = "info", payload: dict[str, Any] | None = None, operational_run_id: uuid.UUID | None = None) -> OperationalEvent:
    event = OperationalEvent(project_id=project_id, operational_run_id=operational_run_id, category=_label(category, 64), event_type=_label(event_type, 96), severity=severity if severity in {"info", "low", "medium", "high", "critical"} else "info", payload_json=sanitize_operational_payload(payload or {}))
    db.add(event)
    await db.flush()
    incr("operational_events_total")
    incr(f"operational_event_{event.category}_total")
    return event


async def run_observer(db: AsyncSession, run: OperationalRun, settings: Settings) -> OperationalRun:
    started = datetime.now(timezone.utc)
    await _start(db, run)
    findings: list[dict[str, Any]] = []
    syncs = list((await db.execute(select(SourceSync).where(SourceSync.project_id == run.project_id).order_by(SourceSync.started_at.desc()).limit(settings.observer_sync_window))).scalars())
    files_seen = sum(int(sync.files_seen or 0) for sync in syncs)
    parser_errors = sum(int(sync.parser_errors or 0) for sync in syncs)
    parser_rate = parser_errors / max(files_seen, 1)
    if parser_errors and parser_rate >= settings.observer_parser_error_rate_threshold:
        findings.append(_finding("parser_loss_spike", "high" if parser_rate >= settings.observer_parser_error_rate_threshold * 2 else "medium", {"syncs_checked": len(syncs), "files_seen": files_seen, "parser_errors": parser_errors, "rate": round(parser_rate, 4)}, {"max_rate": settings.observer_parser_error_rate_threshold}, "Inspect parser_error_reasons and add deterministic parser support before changing retrieval weights."))
    failed_jobs = int((await db.execute(select(func.count()).select_from(IndexJob).where(IndexJob.project_id == run.project_id, IndexJob.status == "failed"))).scalar_one())
    stale_jobs = int((await db.execute(select(func.count()).select_from(IndexJob).where(IndexJob.project_id == run.project_id, IndexJob.status == "stale"))).scalar_one())
    if failed_jobs:
        findings.append(_finding("index_job_failures", "high", {"failed_index_jobs": failed_jobs}, {"allowed": 0}, "Inspect sanitized index-job errors and reingest after the dependency is healthy."))
    if stale_jobs:
        findings.append(_finding("stale_index_version", "medium", {"stale_index_jobs": stale_jobs, "index_version": settings.vector_index_version}, {"allowed": 0}, "Schedule Collector reingestion for the active index version; Core cannot refetch external sources."))
    latest_eval = (await db.execute(select(EvalRun).where(EvalRun.project_id == run.project_id).order_by(EvalRun.created_at.desc()).limit(1))).scalar_one_or_none()
    if latest_eval and latest_eval.summary_json:
        summary = latest_eval.summary_json
        recall = float(summary.get("avg_evidence_recall") or summary.get("avg_recall") or 0.0)
        wrong_rate = float(summary.get("wrong_source_type_rate") or 0.0)
        p95 = float(summary.get("latency_p95_ms") or 0.0)
        if recall < settings.observer_min_eval_recall:
            findings.append(_finding("retrieval_recall_regression", "high", {"eval_run_id": str(latest_eval.id), "avg_evidence_recall": recall}, {"min_recall": settings.observer_min_eval_recall}, "Review chunking and intent budgets against failed eval cases before tuning models."))
        if wrong_rate > settings.observer_max_wrong_source_type_rate:
            findings.append(_finding("wrong_source_type_rate", "medium", {"eval_run_id": str(latest_eval.id), "wrong_source_type_rate": wrong_rate}, {"max_rate": settings.observer_max_wrong_source_type_rate}, "Inspect intent routing and source-type boosts; do not hide mismatches with prompts."))
        if p95 > settings.observer_eval_p95_latency_ms:
            findings.append(_finding("eval_latency_regression", "medium", {"eval_run_id": str(latest_eval.id), "latency_p95_ms": p95}, {"max_latency_p95_ms": settings.observer_eval_p95_latency_ms}, "Review retrieval branches, rerank rate, and evidence-pack size before increasing timeouts."))
    for finding in findings:
        db.add(OperationalFinding(operational_run_id=run.id, project_id=run.project_id, finding_type=finding["finding_type"], severity=finding["severity"], evidence_json=finding["evidence"], threshold_json=finding["threshold"], recommended_action=finding["recommended_action"], fingerprint=finding["fingerprint"]))
    await record_operational_event(db, project_id=run.project_id, operational_run_id=run.id, category="observer", event_type="observer_completed", severity="high" if any(item["severity"] == "high" for item in findings) else "info", payload={"findings_count": len(findings), "syncs_checked": len(syncs)})
    run.summary_json = {"status": "completed", "findings_count": len(findings), "syncs_checked": len(syncs), "parser_error_rate": round(parser_rate, 4), "failed_index_jobs": failed_jobs, "stale_index_jobs": stale_jobs}
    await _finish(db, run)
    incr("operational_runs_total")
    incr("observer_runs_total")
    observe_latency("observer_run", (datetime.now(timezone.utc) - started).total_seconds() * 1000)
    incr("operational_findings_total", len(findings))
    return run


async def run_logging_aggregate(db: AsyncSession, run: OperationalRun, settings: Settings) -> OperationalRun:
    started = datetime.now(timezone.utc)
    await _start(db, run)
    events = list((await db.execute(select(OperationalEvent).where(OperationalEvent.project_id == run.project_id).order_by(OperationalEvent.created_at.desc()).limit(settings.logging_aggregate_event_limit))).scalars())
    run.summary_json = {"status": "completed", "events_checked": len(events), "categories": dict(sorted(Counter(event.category for event in events).items())), "severities": dict(sorted(Counter(event.severity for event in events).items())), "event_types": dict(sorted(Counter(event.event_type for event in events).items())[:20]), "model_summary_used": False}
    await _finish(db, run)
    incr("operational_runs_total")
    incr("logging_runs_total")
    observe_latency("logging_run", (datetime.now(timezone.utc) - started).total_seconds() * 1000)
    return run


async def mark_operational_run_failed(db: AsyncSession, run_id: uuid.UUID, error: str) -> None:
    run = (await db.execute(select(OperationalRun).where(OperationalRun.id == run_id))).scalar_one_or_none()
    if not run or run.status in TERMINAL_STATUSES:
        return
    run.status = OperationalRunStatus.failed
    run.error_code = _label(error, 64)
    run.summary_json = {"status": "failed", "error_code": run.error_code}
    run.finished_at = datetime.now(timezone.utc)
    await record_operational_event(db, project_id=run.project_id, operational_run_id=run.id, category="worker", event_type="operational_run_failed", severity="high", payload={"run_type": run.run_type, "error_code": run.error_code})
    incr("operational_run_failures_total")
    await db.commit()


async def _start(db: AsyncSession, run: OperationalRun) -> None:
    if run.status in TERMINAL_STATUSES:
        return
    run.status = OperationalRunStatus.running
    run.attempts += 1
    run.started_at = datetime.now(timezone.utc)
    await record_operational_event(db, project_id=run.project_id, operational_run_id=run.id, category="worker", event_type="operational_run_started", payload={"run_type": run.run_type, "attempt": run.attempts})
    await db.flush()


async def _finish(db: AsyncSession, run: OperationalRun) -> None:
    run.status = OperationalRunStatus.completed
    run.finished_at = datetime.now(timezone.utc)
    await db.flush()


def sanitize_operational_payload(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:40]:
            normalized = str(key).lower().replace("-", "_")
            if normalized in _DROP_KEYS or any(token in normalized for token in ("secret", "token", "password", "credential", "content", "evidence")):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)[:96]] = sanitize_operational_payload(item, depth=depth + 1)
        return sanitize_audit_metadata(result)
    if isinstance(value, list):
        return [sanitize_operational_payload(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return redact_secrets(value.replace("\n", " ")[:256])
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_secrets(str(value)[:256])


def _finding(finding_type: str, severity: str, evidence: dict[str, Any], threshold: dict[str, Any], recommended_action: str) -> dict[str, Any]:
    fingerprint = hashlib.sha256(json.dumps({"finding_type": finding_type, "threshold": threshold}, sort_keys=True).encode("utf-8")).hexdigest()
    return {"finding_type": finding_type, "severity": severity, "evidence": sanitize_operational_payload(evidence), "threshold": sanitize_operational_payload(threshold), "recommended_action": redact_secrets(recommended_action[:512]), "fingerprint": fingerprint}


def _label(value: str, limit: int) -> str:
    return "".join(char for char in str(value).lower() if char.isalnum() or char in "_-.")[:limit] or "unknown"
