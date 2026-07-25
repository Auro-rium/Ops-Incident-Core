from __future__ import annotations

from pathlib import Path

from incidentops.collector.hints import build
from incidentops.collector.metadata import extract
from incidentops.collector.redaction import redact
from incidentops.collector.service import CollectorService
from incidentops.collector.taxonomy import classify
from incidentops.config.settings import Settings


def test_collector_classifies_core_compatible_source_types():
    assert classify("service/history/handler.go") == "code"
    assert classify("api/history/v1/service.proto") == "api_doc"
    assert classify("deploy/release.yaml") == "deploy"
    assert classify("logs/worker.log") == "logs"
    assert classify("docs/runbook.md") == "runbook"


def test_collector_redacts_before_normalized_payload():
    content, count = redact("token=sk-abcdefghijklmnopqrstuvwxyz0123456789")
    assert count == 1
    assert "sk-" not in content
    assert "[REDACTED:openai_key]" in content


def test_collector_go_and_proto_metadata_and_hints_are_deterministic():
    go_text = "package history\n\ntype Engine struct{}\n\nfunc (e *Engine) Start() {}\n"
    go_metadata = extract("service/history/engine.go", go_text, repo_name="temporal")
    go_hints = build("service/history/engine.go", go_text, "code")
    assert go_metadata["package_name"] == "history"
    assert set(go_metadata["symbol_names"]) == {"Engine", "Start"}
    assert {hint["type"] for hint in go_hints} == {"go_symbol"}

    proto_text = "service History {\n rpc GetHistory(Request) returns (Response);\n}\nmessage Request {}\n"
    proto_metadata = extract("api/history.proto", proto_text)
    proto_hints = build("api/history.proto", proto_text, "api_doc")
    assert {"History", "GetHistory", "Request"}.issubset(proto_metadata["symbol_names"])
    assert {hint["type"] for hint in proto_hints} == {"proto_symbol"}


def test_collector_inspection_reports_generated_and_unsupported_files(tmp_path: Path):
    (tmp_path / "service.go").write_text("package demo\nfunc Run() {}\n", encoding="utf-8")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_text("generated", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\x00")
    summary = CollectorService(Settings()).inspect(tmp_path)
    assert summary.files_seen == 3
    assert summary.source_type_counts["code"] == 1
    assert summary.skipped_reasons["generated_file"] == 1
    assert summary.skipped_reasons["unsupported_extension"] == 1
