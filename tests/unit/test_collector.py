from __future__ import annotations

from pathlib import Path

import httpx

from incidentops.collector.client import CoreCollectorClient
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


def test_collector_metadata_contains_repository_and_explainable_evidence_facts():
    content = (
        "# Deploy Guide\n"
        "GET /v1/orders\n"
        "release 2026.07\n"
        "2026-07-01T12:00:00Z ERROR trace_id=tr-1 request_id=req-1 ERR_TIMEOUT\n"
    )
    metadata = extract(
        "services/orders/docs/deploy.md",
        content,
        repo_name="orders-api",
        branch="main",
        commit_sha="abcdef123456",
    )
    assert metadata["repo_name"] == "orders-api"
    assert metadata["branch"] == "main"
    assert metadata["commit_sha"] == "abcdef123456"
    assert metadata["package_path"] == "services/orders/docs"
    assert metadata["service_name"] == "orders"
    assert metadata["endpoint_candidates"] == ["/v1/orders"]
    assert metadata["log_levels"] == ["ERROR"]
    assert metadata["error_codes"] == ["ERR_TIMEOUT"]
    assert metadata["trace_ids"] == ["tr-1"]
    assert metadata["request_ids"] == ["req-1"]


def test_collector_summary_merges_per_batch_core_diagnostics_without_raw_content():
    from incidentops.collector.models import CollectorSummary

    summary = CollectorSummary()
    summary.record_core_diagnostics(
        {
            "last_batch_error_count": 2,
            "last_batch_parser_error_reasons": {"malformed_content": 1, "embedding_failed": 1},
            "last_batch_chunk_discard_reasons": {"chunk_limit_exceeded": 1},
            "last_batch_embedding_failures": 1,
        }
    )
    diagnostics = summary.diagnostics()
    assert diagnostics["parser_error_count"] == 2
    assert diagnostics["parser_error_reasons"] == {"embedding_failed": 1, "malformed_content": 1}
    assert diagnostics["chunk_discard_reasons"] == {"chunk_limit_exceeded": 1}
    assert diagnostics["embedding_failures"] == 1
    assert "content" not in diagnostics


def test_collector_source_registration_accepts_core_id_response():
    client = CoreCollectorClient("http://core.test", "test-token")
    client._client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(201, json={"id": "source-123"})),
        base_url="http://core.test",
    )
    try:
        assert client.register_source("project-123", "source") == "source-123"
    finally:
        client.close()


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


def test_collector_inspection_honors_explicit_benchmark_scope(tmp_path: Path):
    (tmp_path / "README.md").write_text("# Read me", encoding="utf-8")
    (tmp_path / "service").mkdir()
    (tmp_path / "service" / "history.go").write_text("package service", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "helper.go").write_text("package tools", encoding="utf-8")
    summary = CollectorService(Settings()).inspect(
        tmp_path,
        include_paths=["README.md", "service/**", "tools/**"],
        exclude_paths=["tools/**"],
    )
    assert summary.files_seen == 2
    assert summary.source_type_counts["code"] == 1
