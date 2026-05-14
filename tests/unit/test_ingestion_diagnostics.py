from __future__ import annotations

from pathlib import Path

from incidentops.ingestion.diagnostics import build_source_coverage, inspect_folder


FIXTURE_PATH = str((Path(__file__).resolve().parents[1] / "fixtures" / "basic_incident").resolve())


def test_build_source_coverage_warns_for_missing_sources():
    coverage = build_source_coverage({"runbook": 1})
    assert coverage["has_runbooks"] is True
    assert coverage["has_deploys"] is False
    assert any("Deploy-regression" in item for item in coverage["warnings"])


def test_inspect_folder_reports_fixture_counts():
    report = inspect_folder(FIXTURE_PATH)
    assert report["total_files_seen"] > 0
    assert report["ingestable_files"] > 0
    assert report["estimated_ingestability"] in {"high", "medium", "low"}
    assert report["source_coverage"]["has_logs"] is True
