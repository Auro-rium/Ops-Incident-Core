from __future__ import annotations

import uuid

from incidentops.readiness.service import LatestSyncSnapshot, ReadinessSnapshot, build_readiness_report


def _snapshot(**overrides) -> ReadinessSnapshot:
    defaults = {
        "project_id": uuid.uuid4(),
        "source_count": 1,
        "collector_count": 1,
        "document_count": 10,
        "chunk_count": 25,
        "successful_syncs": 1,
        "failed_syncs": 0,
        "source_type_counts": {},
        "chunk_type_counts": {},
        "latest_sync": LatestSyncSnapshot(status="success", documents_received=10, chunks_created=25),
    }
    defaults.update(overrides)
    return ReadinessSnapshot(**defaults)


def test_empty_project_scores_zero():
    report = build_readiness_report(
        _snapshot(
            source_count=0,
            collector_count=0,
            document_count=0,
            chunk_count=0,
            successful_syncs=0,
            source_type_counts={},
            latest_sync=None,
        )
    )

    assert report["score"] == 0
    assert report["grade"] == "empty"
    assert "collector sync" in " ".join(report["suggested_actions"]).lower()


def test_code_docs_only_is_partial_and_runtime_questions_are_weak():
    report = build_readiness_report(
        _snapshot(
            source_type_counts={"code": 7, "config": 2, "unknown_text": 1},
            chunk_type_counts={"markdown_section": 3, "function": 10},
        )
    )

    assert report["coverage"]["has_code"] is True
    assert report["coverage"]["has_docs"] is True
    assert report["coverage"]["has_logs"] is False
    assert report["score"] <= 55
    assert report["grade"] in {"weak", "partial"}
    assert any("latency" in item.lower() for item in report["weak_questions"])
    assert any("logs" in item.lower() for item in report["missing_evidence"])


def test_full_incident_evidence_scores_high():
    report = build_readiness_report(
        _snapshot(
            source_type_counts={
                "code": 10,
                "config": 3,
                "logs": 4,
                "deploy": 2,
                "runbook": 2,
                "incident": 1,
                "api_doc": 1,
            }
        )
    )

    assert report["score"] >= 85
    assert report["grade"] == "excellent"
    assert report["missing_evidence"] == []


def test_failed_latest_sync_and_parser_errors_lower_score_and_warn():
    report = build_readiness_report(
        _snapshot(
            source_type_counts={"code": 5, "logs": 3, "deploy": 1},
            latest_sync=LatestSyncSnapshot(status="failed", parser_errors=4),
            failed_syncs=1,
        )
    )

    assert report["score"] < 50
    assert any("failed" in item.lower() for item in report["warnings"])
    assert any("parser error" in item.lower() for item in report["warnings"])


def test_missing_deploys_logs_and_incidents_are_reported():
    report = build_readiness_report(_snapshot(source_type_counts={"code": 3, "runbook": 1}))

    missing = " ".join(report["missing_evidence"]).lower()
    assert "logs" in missing
    assert "deploy" in missing
    assert "incident" in missing
    assert any("deploy-regression" in item for item in report["observability_gaps"])
