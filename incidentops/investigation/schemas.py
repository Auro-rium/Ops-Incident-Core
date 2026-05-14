from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class InvestigationEntities:
    service_name: str | None = None
    endpoint: str | None = None
    deploy_hash: str | None = None
    symptom: str | None = None
    time_window: dict[str, str] | None = None
    raw_matches: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class TimelineEvent:
    event_type: str
    summary: str
    ts: datetime | None = None
    evidence_chunk_ids: list[str] = field(default_factory=list)


@dataclass
class Hypothesis:
    hypothesis_id: str
    summary: str
    score: float
    confidence: str
    evidence_chunk_ids: list[str]
    supporting_reasons: list[str]
    contradicting_reasons: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class InvestigationResult:
    question: str
    task_type: str
    entities: InvestigationEntities
    timeline: list[TimelineEvent]
    hypotheses: list[Hypothesis]
    likely_root_cause: Hypothesis
    confidence: str
    confidence_reasons: list[str]
    affected_services: list[str]
    suggested_fix: str | None
    citations: list[dict]
    missing_data: list[str]
    unknowns: list[str]
    evidence: list[dict]
    debug: dict[str, Any] = field(default_factory=dict)
