from __future__ import annotations

from datetime import datetime

from incidentops.investigation.classifier import classify_task
from incidentops.investigation.entity_extractor import extract_entities
from incidentops.investigation.hypothesis_generator import generate_hypotheses
from incidentops.investigation.service import _build_cited_answer, _score_confidence
from incidentops.investigation.root_cause_selector import select_root_cause
from incidentops.investigation.timeline_builder import build_timeline
from apps.api.routes.answer import _skip_rerank_for_intent


def test_classifier_supports_generic_task_types():
    assert classify_task("Why did latency increase after the last deploy?") == "latency_investigation"
    assert classify_task("Did a deploy cause this regression?") == "deploy_regression"
    assert classify_task("Have we seen this before?") == "generic_incident_question"
    assert classify_task("Where is the history service implemented?") == "generic_incident_question"


def test_entity_extractor_detects_generic_entities():
    entities = extract_entities("Why did GET /v1/orders slow down after deploy abc1234?")
    assert entities.endpoint == "/v1/orders"
    assert entities.deploy_hash == "abc1234"
    assert entities.symptom == "latency"


def test_timeline_orders_by_timestamp():
    evidence = [
        {
            "chunk_id": "deploy1",
            "source_type": "deploy",
            "deploy_hash": "abc1234",
            "timestamp_start": datetime(2026, 5, 1, 9, 55),
            "text": "deploy happened",
        },
        {
            "chunk_id": "log1",
            "source_type": "logs",
            "timestamp_start": datetime(2026, 5, 1, 10, 5),
            "text": "timeout after 1500ms",
        },
    ]
    timeline = build_timeline(evidence, {}, {"diff_count": 1}, [])
    assert timeline[0].event_type == "deploy"
    assert timeline[1].event_type == "log_signal"


def test_weak_evidence_response_is_honest():
    hypotheses = generate_hypotheses({}, [{"chunk_id": "x", "source_type": "runbook"}], {"slow_requests": 0, "errors": 0, "timeouts": 0, "services": []}, {"diff_count": 0}, [])
    selected = select_root_cause(hypotheses, [])
    assert selected.summary == "Evidence is insufficient to identify a confident root cause."


def test_confidence_scoring_is_low_when_key_sources_missing():
    entities = extract_entities("Why did latency increase after deploy abc1234?")
    confidence, reasons = _score_confidence(
        query_intent="runtime_incident",
        supported=False,
        support_reasons=["root-cause investigation requires logs or deploy/change evidence"],
        entities=entities,
        evidence=[{"source_type": "runbook", "deploy_hash": None, "endpoint": None}],
        log_findings={"timestamped_logs": False},
        deploy_findings={"diff_count": 0},
        previous_incidents=[],
        missing_data=["deployment history not found", "logs missing timestamps", "no code diff found"],
    )
    assert confidence == "low"
    assert reasons


def test_lookup_and_unsupported_runtime_queries_skip_remote_reranking():
    assert _skip_rerank_for_intent("code_location", supported=False) is True
    assert _skip_rerank_for_intent("runtime_incident", supported=False) is True
    assert _skip_rerank_for_intent("runtime_incident", supported=True) is False


def test_investigation_builds_bounded_cited_answer_when_rca_is_unsupported():
    answer = _build_cited_answer(
        [{"citation": {"label": "[1]"}, "document_path": "backend/app/main.py"}],
        type("Selected", (), {"confidence": "low", "summary": "weak evidence"})(),
        False,
        ["logs missing timestamps"],
    )

    assert "backend/app/main.py" in answer
    assert "[1]" in answer
    assert "not a confident root-cause" in answer
