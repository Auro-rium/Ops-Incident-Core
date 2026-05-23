from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.db.models import Chunk, Collector, Document, Source, SourceSync


SOURCE_TYPES = ("code", "logs", "runbook", "deploy", "incident", "api_doc", "config", "unknown_text")
SUCCESS_STATUSES = {"success", "partial_success"}
FAILED_STATUSES = {"failed", "cancelled"}


@dataclass
class LatestSyncSnapshot:
    sync_id: str | None = None
    status: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    documents_received: int = 0
    chunks_created: int = 0
    skipped_unchanged: int = 0
    parser_errors: int = 0
    duration_seconds: float | None = None


@dataclass
class ReadinessSnapshot:
    project_id: uuid.UUID
    source_count: int = 0
    collector_count: int = 0
    document_count: int = 0
    chunk_count: int = 0
    successful_syncs: int = 0
    failed_syncs: int = 0
    source_type_counts: dict[str, int] = field(default_factory=dict)
    chunk_type_counts: dict[str, int] = field(default_factory=dict)
    latest_sync: LatestSyncSnapshot | None = None
    coverage_hints: dict[str, Any] = field(default_factory=dict)


async def build_project_readiness(db: AsyncSession, project_id: uuid.UUID) -> dict[str, Any]:
    snapshot = await load_readiness_snapshot(db, project_id)
    return build_readiness_report(snapshot)


async def load_readiness_snapshot(db: AsyncSession, project_id: uuid.UUID) -> ReadinessSnapshot:
    source_count = await _count(db, select(func.count()).select_from(Source).where(Source.project_id == project_id))
    collector_count = await _count(db, select(func.count()).select_from(Collector).where(Collector.project_id == project_id))
    document_count = await _count(db, select(func.count()).select_from(Document).where(Document.project_id == project_id))
    chunk_count = await _count(db, select(func.count()).select_from(Chunk).where(Chunk.project_id == project_id))

    sync_status_rows = (
        await db.execute(
            select(SourceSync.status, func.count())
            .where(SourceSync.project_id == project_id)
            .group_by(SourceSync.status)
        )
    ).all()
    sync_status_counts = {str(status or ""): int(count or 0) for status, count in sync_status_rows}

    source_type_counts = await _group_counts(
        db,
        select(func.coalesce(Document.source_type, Document.doc_type), func.count())
        .where(Document.project_id == project_id)
        .group_by(func.coalesce(Document.source_type, Document.doc_type)),
    )
    chunk_type_counts = await _group_counts(
        db,
        select(Chunk.chunk_type, func.count()).where(Chunk.project_id == project_id).group_by(Chunk.chunk_type),
    )

    latest_sync_result = await db.execute(
        select(SourceSync)
        .where(SourceSync.project_id == project_id)
        .order_by(SourceSync.started_at.desc())
        .limit(1)
    )
    latest_sync_model = latest_sync_result.scalar_one_or_none()
    latest_sync = _latest_sync_snapshot(latest_sync_model) if latest_sync_model else None

    coverage_hints: dict[str, Any] = {}
    if latest_sync_model and latest_sync_model.coverage_json:
        coverage_hints.update(latest_sync_model.coverage_json)

    return ReadinessSnapshot(
        project_id=project_id,
        source_count=source_count,
        collector_count=collector_count,
        document_count=document_count,
        chunk_count=chunk_count,
        successful_syncs=sum(sync_status_counts.get(status, 0) for status in SUCCESS_STATUSES),
        failed_syncs=sum(sync_status_counts.get(status, 0) for status in FAILED_STATUSES),
        source_type_counts=dict(source_type_counts),
        chunk_type_counts=dict(chunk_type_counts),
        latest_sync=latest_sync,
        coverage_hints=coverage_hints,
    )


def build_readiness_report(snapshot: ReadinessSnapshot) -> dict[str, Any]:
    source_type_counts = _normalized_counts(snapshot.source_type_counts)
    chunk_type_counts = _normalized_counts(snapshot.chunk_type_counts)
    coverage = _coverage(source_type_counts, chunk_type_counts, snapshot.coverage_hints)
    score, score_notes = _score(snapshot, coverage)
    grade = _grade(score)
    missing_evidence = _missing_evidence(coverage)
    answerable_questions = _answerable_questions(coverage)
    weak_questions = _weak_questions(coverage)
    observability_gaps = _observability_gaps(coverage)
    suggested_actions = _suggested_actions(coverage, snapshot)
    suggested_questions = _suggested_questions(coverage)
    warnings = _warnings(snapshot, coverage, score_notes)

    return {
        "project_id": str(snapshot.project_id),
        "score": score,
        "grade": grade,
        "summary": _summary(score, coverage, snapshot),
        "source_count": snapshot.source_count,
        "collector_count": snapshot.collector_count,
        "latest_sync": _latest_sync_dict(snapshot.latest_sync),
        "coverage": coverage,
        "counts": {
            "documents": snapshot.document_count,
            "chunks": snapshot.chunk_count,
            "sources": snapshot.source_count,
            "successful_syncs": snapshot.successful_syncs,
            "failed_syncs": snapshot.failed_syncs,
        },
        "source_type_counts": {key: source_type_counts.get(key, 0) for key in SOURCE_TYPES},
        "answerable_questions": answerable_questions,
        "weak_questions": weak_questions,
        "missing_evidence": missing_evidence,
        "observability_gaps": observability_gaps,
        "suggested_actions": suggested_actions,
        "suggested_questions": suggested_questions,
        "warnings": warnings,
        "readiness_generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def _count(db: AsyncSession, statement) -> int:
    result = await db.execute(statement)
    return int(result.scalar_one() or 0)


async def _group_counts(db: AsyncSession, statement) -> Counter[str]:
    result = await db.execute(statement)
    counts: Counter[str] = Counter()
    for key, count in result.all():
        counts[str(key or "unknown_text").lower()] += int(count or 0)
    return counts


def _latest_sync_snapshot(sync: SourceSync) -> LatestSyncSnapshot:
    diagnostics = sync.diagnostics_json or {}
    duration_seconds = None
    if sync.started_at and sync.finished_at:
        duration_seconds = max((sync.finished_at - sync.started_at).total_seconds(), 0)
    return LatestSyncSnapshot(
        sync_id=str(sync.id),
        status=sync.status,
        started_at=sync.started_at,
        finished_at=sync.finished_at,
        documents_received=sync.documents_received,
        chunks_created=sync.chunks_created,
        skipped_unchanged=int(diagnostics.get("skipped_unchanged", 0) or 0),
        parser_errors=sync.parser_errors,
        duration_seconds=duration_seconds,
    )


def _normalized_counts(counts: dict[str, int]) -> Counter[str]:
    normalized: Counter[str] = Counter()
    for key, value in counts.items():
        lowered = str(key or "unknown_text").lower()
        if lowered in {"deploy_history", "patch"}:
            lowered = "deploy"
        if lowered in {"incidents", "incident_report"}:
            lowered = "incident"
        if lowered in {"runbooks", "runbook"}:
            lowered = "runbook"
        if lowered in {"logs_folder", "log"}:
            lowered = "logs"
        normalized[lowered] += int(value or 0)
    return normalized


def _coverage(source_type_counts: Counter[str], chunk_type_counts: Counter[str], hints: dict[str, Any]) -> dict[str, bool]:
    has_markdown = any("markdown" in key for key in chunk_type_counts)
    has_text_docs = bool(source_type_counts.get("unknown_text", 0)) or bool(source_type_counts.get("runbook", 0))
    has_docs = bool(hints.get("has_docs")) or has_markdown or has_text_docs
    return {
        "has_code": bool(hints.get("has_code")) or source_type_counts.get("code", 0) > 0,
        "has_docs": has_docs,
        "has_logs": bool(hints.get("has_logs")) or source_type_counts.get("logs", 0) > 0,
        "has_deploys": bool(hints.get("has_deploys")) or source_type_counts.get("deploy", 0) > 0,
        "has_runbooks": bool(hints.get("has_runbooks")) or source_type_counts.get("runbook", 0) > 0,
        "has_incidents": bool(hints.get("has_incidents")) or source_type_counts.get("incident", 0) > 0,
        "has_api_docs": bool(hints.get("has_api_docs")) or source_type_counts.get("api_doc", 0) > 0,
        "has_configs": bool(hints.get("has_configs")) or source_type_counts.get("config", 0) > 0,
    }


def _score(snapshot: ReadinessSnapshot, coverage: dict[str, bool]) -> tuple[int, list[str]]:
    if snapshot.document_count == 0:
        return 0, ["No documents have been indexed."]

    score = 0
    score += 15 if coverage["has_code"] else 0
    score += 10 if coverage["has_docs"] else 0
    score += 10 if coverage["has_configs"] else 0
    score += 20 if coverage["has_logs"] else 0
    score += 15 if coverage["has_deploys"] else 0
    score += 15 if coverage["has_runbooks"] else 0
    score += 10 if coverage["has_incidents"] else 0
    score += 5 if coverage["has_api_docs"] else 0

    notes: list[str] = []
    if snapshot.successful_syncs == 0:
        score -= 30
        notes.append("No successful sync has completed.")
    latest = snapshot.latest_sync
    if latest and latest.status in FAILED_STATUSES:
        score -= 20
        notes.append("Latest sync failed.")
    parser_errors = latest.parser_errors if latest else 0
    if parser_errors:
        penalty = min(15, max(3, parser_errors * 2))
        score -= penalty
        notes.append(f"Latest sync reported {parser_errors} parser error(s).")
    if snapshot.chunk_count == 0:
        score = min(score, 20)
        notes.append("No chunks are available for retrieval.")
    if coverage["has_code"] and coverage["has_docs"] and not any(
        coverage[key] for key in ("has_logs", "has_deploys", "has_runbooks", "has_incidents")
    ):
        score = min(score, 55)
        notes.append("Only code/docs evidence is present; runtime incident investigation will be weak.")
    return max(0, min(100, int(score))), notes


def _grade(score: int) -> str:
    if score >= 85:
        return "excellent"
    if score >= 70:
        return "good"
    if score >= 50:
        return "partial"
    if score >= 25:
        return "weak"
    return "empty"


def _missing_evidence(coverage: dict[str, bool]) -> list[str]:
    items = []
    if not coverage["has_logs"]:
        items.append("Timestamped application logs are missing.")
    if not coverage["has_deploys"]:
        items.append("Deploy history or code diffs are missing.")
    if not coverage["has_runbooks"]:
        items.append("Runbooks or remediation docs are missing.")
    if not coverage["has_incidents"]:
        items.append("Previous incident reports or postmortems are missing.")
    if not coverage["has_api_docs"]:
        items.append("API/OpenAPI docs are missing; endpoint investigation may rely on code only.")
    if not coverage["has_code"]:
        items.append("Source code evidence is missing.")
    return items


def _answerable_questions(coverage: dict[str, bool]) -> list[str]:
    questions = []
    if coverage["has_code"] or coverage["has_docs"] or coverage["has_configs"]:
        questions.extend(
            [
                "Where is a feature, endpoint, or behavior implemented?",
                "Which files define API, configuration, or service behavior?",
                "Where are service or module boundaries visible in the repo?",
            ]
        )
    if coverage["has_logs"]:
        questions.append("What errors, timeouts, or latency symptoms appear in the logs?")
    if coverage["has_deploys"]:
        questions.append("What deploys, releases, or diffs are available around a change?")
    if coverage["has_runbooks"]:
        questions.append("What remediation or rollback steps are already documented?")
    if coverage["has_incidents"]:
        questions.append("Have similar incidents or postmortems been indexed?")
    if coverage["has_api_docs"]:
        questions.append("Which endpoints are documented?")
    return questions


def _weak_questions(coverage: dict[str, bool]) -> list[str]:
    questions = []
    if not coverage["has_logs"]:
        questions.extend(
            [
                "Why did latency spike at runtime?",
                "What errors happened during the outage?",
            ]
        )
    if not coverage["has_deploys"]:
        questions.extend(
            [
                "Was this caused by the last deploy?",
                "What changed immediately before the incident?",
            ]
        )
    if not coverage["has_incidents"]:
        questions.append("Has this happened before?")
    if not coverage["has_runbooks"]:
        questions.append("What rollback or fix does the team already recommend?")
    return questions


def _observability_gaps(coverage: dict[str, bool]) -> list[str]:
    gaps = []
    if not coverage["has_logs"]:
        gaps.append("No timestamped logs found; runtime symptoms cannot be grounded.")
    if not coverage["has_deploys"]:
        gaps.append("No deploy history found; deploy-regression analysis will be weak.")
    if coverage["has_logs"] and not coverage["has_deploys"]:
        gaps.append("Logs exist but deploy context is missing.")
    if not coverage["has_incidents"]:
        gaps.append("No previous incident reports found; similarity lookup is unavailable.")
    return gaps


def _suggested_actions(coverage: dict[str, bool], snapshot: ReadinessSnapshot) -> list[str]:
    actions = []
    if snapshot.source_count == 0:
        actions.append("Register a source and run Collector sync.")
    if not coverage["has_logs"]:
        actions.append("Add timestamped application logs with service names, request IDs, and error levels.")
    if not coverage["has_deploys"]:
        actions.append("Add deploy history, release notes, or patch/diff exports.")
    if not coverage["has_runbooks"]:
        actions.append("Add runbooks for critical services and common rollback procedures.")
    if not coverage["has_incidents"]:
        actions.append("Add previous incident reports or postmortems.")
    if not coverage["has_api_docs"]:
        actions.append("Add OpenAPI/API docs if endpoint-level investigation matters.")
    if not coverage["has_code"]:
        actions.append("Add backend source code or service ownership docs.")
    return actions


def _suggested_questions(coverage: dict[str, bool]) -> list[str]:
    questions = []
    if coverage["has_code"]:
        questions.extend(
            [
                "Where is the application configured?",
                "Which files define database configuration?",
                "Which modules or services appear most important?",
            ]
        )
    if coverage["has_configs"]:
        questions.append("Which configuration files mention deployment, Docker, or database settings?")
    if coverage["has_logs"]:
        questions.append("What timeout, error, or latency symptoms appear in the logs?")
    if coverage["has_deploys"]:
        questions.append("What changed in the latest deploy history or diff?")
    if coverage["has_runbooks"]:
        questions.append("Which runbooks mention rollback or restart steps?")
    if not coverage["has_logs"] or not coverage["has_deploys"]:
        questions.append("What evidence is missing for deploy-regression or latency investigation?")
    return questions


def _warnings(snapshot: ReadinessSnapshot, coverage: dict[str, bool], score_notes: list[str]) -> list[str]:
    warnings = list(score_notes)
    latest = snapshot.latest_sync
    if latest and latest.status == "partial_success":
        warnings.append("Latest sync completed with partial success; inspect parser errors and skipped documents.")
    if latest and latest.status in FAILED_STATUSES:
        warnings.append("Latest sync failed; readiness may be stale.")
    if not coverage["has_logs"]:
        warnings.append("Latency and outage investigations will be weak without logs.")
    if not coverage["has_deploys"]:
        warnings.append("Deploy-regression investigations will be weak without deploy history.")
    if not coverage["has_incidents"]:
        warnings.append("Previous-incident lookup is unavailable without incident reports.")
    return _dedupe(warnings)


def _summary(score: int, coverage: dict[str, bool], snapshot: ReadinessSnapshot) -> str:
    if snapshot.document_count == 0:
        return "No indexed evidence is available yet. Run Collector sync before asking incident questions."
    found = [
        label
        for label, present in [
            ("code", coverage["has_code"]),
            ("docs", coverage["has_docs"]),
            ("configs", coverage["has_configs"]),
            ("logs", coverage["has_logs"]),
            ("deploy history", coverage["has_deploys"]),
            ("runbooks", coverage["has_runbooks"]),
            ("incidents", coverage["has_incidents"]),
            ("API docs", coverage["has_api_docs"]),
        ]
        if present
    ]
    missing = _missing_evidence(coverage)
    if score >= 70:
        return f"Evidence coverage is strong across {', '.join(found)}."
    if missing:
        return f"Found {', '.join(found) or 'limited evidence'}, but {missing[0]}"
    return f"Found {', '.join(found) or 'limited evidence'}."


def _latest_sync_dict(latest: LatestSyncSnapshot | None) -> dict[str, Any]:
    if not latest:
        return {
            "sync_id": None,
            "status": None,
            "started_at": None,
            "finished_at": None,
            "documents_received": 0,
            "chunks_created": 0,
            "skipped_unchanged": 0,
            "parser_errors": 0,
            "duration_seconds": None,
        }
    return {
        "sync_id": latest.sync_id,
        "status": latest.status,
        "started_at": latest.started_at,
        "finished_at": latest.finished_at,
        "documents_received": latest.documents_received,
        "chunks_created": latest.chunks_created,
        "skipped_unchanged": latest.skipped_unchanged,
        "parser_errors": latest.parser_errors,
        "duration_seconds": latest.duration_seconds,
    }


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
