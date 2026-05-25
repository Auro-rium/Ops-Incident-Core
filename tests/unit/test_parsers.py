"""
Unit tests for parsers — markdown, code, log, deploy, incident.
"""

from __future__ import annotations

from incidentops.ingestion.parsers.markdown_parser import parse_markdown
from incidentops.ingestion.parsers.code_parser import parse_go, parse_proto, parse_python
from incidentops.ingestion.parsers.log_parser import parse_logs
from incidentops.ingestion.parsers.deploy_parser import parse_deploy_history, parse_patch_file
from incidentops.ingestion.parsers.incident_parser import parse_incident


# ── Markdown parser ───────────────────────────

class TestMarkdownParser:
    def test_basic_heading_split(self):
        md = "# Title\nIntro\n## Section A\nContent A\n## Section B\nContent B"
        chunks = parse_markdown(md, "test.md")
        assert len(chunks) == 3
        assert any("Content A" in c.text for c in chunks)

    def test_preserves_section_title(self):
        md = "# Overview\nSome text\n## Details\nMore text"
        chunks = parse_markdown(md, "test.md")
        titles = [c.section_title for c in chunks]
        assert "Overview" in titles or "Details" in titles

    def test_preserves_line_numbers(self):
        md = "# Title\nLine 2\n## Section\nLine 4"
        chunks = parse_markdown(md, "test.md")
        assert all(c.start_line is not None for c in chunks)
        assert all(c.end_line is not None for c in chunks)

    def test_empty_content(self):
        chunks = parse_markdown("", "test.md")
        assert len(chunks) == 0

    def test_chunk_type(self):
        md = "# Test\nContent"
        chunks = parse_markdown(md, "test.md")
        assert all(c.chunk_type == "markdown_section" for c in chunks)

    def test_source_type_propagated(self):
        md = "# Test\nContent"
        chunks = parse_markdown(md, "test.md", source_type="runbook")
        assert all(c.source_type == "runbook" for c in chunks)


# ── Code parser ───────────────────────────────

class TestCodeParser:
    def test_extracts_functions(self):
        code = '''
def hello():
    """Say hello."""
    return "hello"

def goodbye():
    """Say goodbye."""
    return "bye"
'''
        chunks = parse_python(code, "test.py")
        func_chunks = [c for c in chunks if "def " in (c.section_title or "")]
        assert len(func_chunks) >= 2

    def test_extracts_classes(self):
        code = '''
class MyClass:
    """A test class."""
    def method(self):
        pass
'''
        chunks = parse_python(code, "test.py")
        class_chunks = [c for c in chunks if "class " in (c.section_title or "")]
        assert len(class_chunks) >= 1

    def test_preserves_line_numbers(self):
        code = "def foo():\n    pass\n\ndef bar():\n    pass"
        chunks = parse_python(code, "test.py")
        assert all(c.start_line is not None for c in chunks)

    def test_service_name_propagated(self):
        code = "def foo():\n    pass"
        chunks = parse_python(code, "test.py", service_name="checkout")
        assert all(c.service_name == "checkout" for c in chunks)

    def test_handles_syntax_error(self):
        code = "this is not valid python {{{}"
        chunks = parse_python(code, "broken.py")
        assert len(chunks) == 1  # fallback whole-file chunk

    def test_extracts_go_functions_and_types(self):
        code = """package history

type Handler struct {
    store Store
}

func NewHandler(store Store) *Handler {
    return &Handler{store: store}
}

func (h *Handler) StartWorkflowTask() error {
    return nil
}
"""
        chunks = parse_go(code, "service/history/handler.go", service_name="history")
        titles = [chunk.section_title for chunk in chunks]
        assert "type Handler" in titles
        assert "function NewHandler" in titles
        assert "function StartWorkflowTask" in titles
        assert all(chunk.source_type == "code" for chunk in chunks)
        assert all(chunk.start_line is not None and chunk.end_line is not None for chunk in chunks)
        assert chunks[-1].metadata["language"] == "go"

    def test_extracts_proto_services_and_messages(self):
        proto = """syntax = "proto3";

package temporal.server.api.historyservice.v1;

service HistoryService {
  rpc StartWorkflowExecution(StartWorkflowExecutionRequest) returns (StartWorkflowExecutionResponse);
}

message StartWorkflowExecutionRequest {
  string namespace = 1;
}
"""
        chunks = parse_proto(proto, "proto/temporal/server/api/historyservice/v1/service.proto")
        titles = [chunk.section_title for chunk in chunks]
        assert "service HistoryService" in titles
        assert "message StartWorkflowExecutionRequest" in titles
        assert all(chunk.source_type == "api_doc" for chunk in chunks)
        assert all(chunk.chunk_type == "api_endpoint" or chunk.chunk_type == "proto_preamble" for chunk in chunks)


# ── Log parser ────────────────────────────────

class TestLogParser:
    def test_parses_structured_logs(self):
        log = (
            "2026-04-12T10:00:01.123Z INFO  checkout.service [trace_id=tr_001] "
            "[deploy=8f13a2] POST /checkout cart_id=cart_901 — 1687ms\n"
            "2026-04-12T10:00:02.456Z WARN  checkout.service [trace_id=tr_002] "
            "[deploy=8f13a2] Slow request\n"
        )
        chunks = parse_logs(log, "checkout.log")
        assert len(chunks) >= 1
        chunk = chunks[0]
        assert chunk.chunk_type == "log_window"
        assert chunk.deploy_hash == "8f13a2"

    def test_extracts_trace_ids(self):
        log = "2026-04-12T10:00:01.123Z INFO  svc [trace_id=tr_001] message\n"
        chunks = parse_logs(log, "test.log")
        assert chunks[0].metadata.get("trace_ids") == ["tr_001"]

    def test_infers_service_from_path(self):
        log = "2026-04-12T10:00:01.123Z INFO  svc message\n"
        chunks = parse_logs(log, "logs/checkout-errors.log")
        assert chunks[0].service_name == "checkout"

    def test_windowed_chunking(self):
        lines = [f"2026-04-12T10:00:{i:02d}.000Z INFO svc msg{i}" for i in range(60)]
        log = "\n".join(lines)
        chunks = parse_logs(log, "big.log", window_size=30)
        assert len(chunks) == 2  # 60 lines / 30 window = 2 chunks


# ── Deploy parser ─────────────────────────────

class TestDeployParser:
    def test_parses_deploy_history(self):
        json_content = '''[
            {"deploy_hash": "8f13a2", "service_name": "checkout",
             "commit_sha": "abc123", "deployed_at": "2026-04-12T09:50:00Z",
             "author": "charlie", "summary": "Add inventory validation",
             "changed_files": ["service.py"]}
        ]'''
        chunks = parse_deploy_history(json_content, "deploy-history.json")
        assert len(chunks) == 1
        assert chunks[0].deploy_hash == "8f13a2"
        assert chunks[0].service_name == "checkout"
        assert chunks[0].chunk_type == "deploy_diff"

    def test_parses_single_deploy_object_with_collector_aliases(self):
        json_content = """{
            "service": "orders",
            "commit_sha": "abcdef1234567890",
            "deployed_at": "2026-05-05T09:59:00Z"
        }"""
        chunks = parse_deploy_history(json_content, "deploys/deploy-history.json")
        assert len(chunks) == 1
        assert chunks[0].deploy_hash == "abcdef1234567890"
        assert chunks[0].service_name == "orders"
        assert chunks[0].chunk_type == "deploy_diff"

    def test_parses_patch_file(self):
        patch = (
            "diff --git a/services/checkout/service.py b/services/checkout/service.py\n"
            "index 1a2b3c4..5d6e7f8 100644\n"
            "--- a/services/checkout/service.py\n"
            "+++ b/services/checkout/service.py\n"
            "@@ -55,6 +55,8 @@\n"
            "+    validate_items_sync(cart.items)\n"
        )
        chunks = parse_patch_file(patch, "diff-8f13a2.patch")
        assert len(chunks) >= 1
        assert chunks[0].deploy_hash == "8f13a2"
        assert chunks[0].service_name == "checkout"

    def test_invalid_json(self):
        chunks = parse_deploy_history("not json", "bad.json")
        assert chunks == []


# ── Incident parser ───────────────────────────

class TestIncidentParser:
    def test_parses_sections(self):
        md = (
            "# Incident: Checkout Timeout\n"
            "## Summary\nCheckout was slow\n"
            "## Root Cause\nSync inventory call\n"
            "## Fix\nRolled back\n"
        )
        chunks = parse_incident(md, "incidents/2026-03-14-checkout-timeout.md")
        assert len(chunks) >= 3
        titles = [c.section_title for c in chunks]
        assert "Summary" in titles
        assert "Root Cause" in titles
        assert "Fix" in titles

    def test_chunk_type(self):
        md = "# Incident\n## Summary\nText"
        chunks = parse_incident(md, "incident.md")
        assert all(c.chunk_type == "incident_section" for c in chunks)

    def test_extracts_service(self):
        md = "# Checkout Timeout\n## Summary\nCheckout was slow"
        chunks = parse_incident(md, "incident.md")
        assert chunks[0].service_name == "checkout"
