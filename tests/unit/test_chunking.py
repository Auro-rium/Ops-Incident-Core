"""
Unit tests for chunking and metadata extraction.
"""

from __future__ import annotations

from incidentops.ingestion.chunking.chunker import count_tokens, split_text_by_tokens
from incidentops.ingestion.chunking.metadata import (
    classify_doc_type,
    classify_source_type,
    extract_deploy_hash,
    extract_endpoints,
    extract_service_from_path,
)
from incidentops.security.sanitizer import redact_secrets


# ── Token counting ────────────────────────────

class TestChunker:
    def test_count_tokens_basic(self):
        text = "hello world foo bar"
        tokens = count_tokens(text)
        assert 4 <= tokens <= 8  # ~1.3x word count

    def test_split_small_text_no_split(self):
        text = "small text"
        result = split_text_by_tokens(text, max_tokens=512)
        assert len(result) == 1
        assert result[0] == text

    def test_split_large_text(self):
        text = " ".join(["word"] * 1000)
        result = split_text_by_tokens(text, max_tokens=100, overlap_tokens=20)
        assert len(result) > 1


# ── Metadata extraction ──────────────────────

class TestMetadata:
    def test_extract_service_from_path(self):
        assert extract_service_from_path("services/checkout/service.py") == "checkout"
        assert extract_service_from_path("services/inventory/handler.py") == "inventory"
        assert extract_service_from_path("services/orders-api/handler.py") == "orders-api"
        assert extract_service_from_path("libs/utils.py") is None

    def test_extract_deploy_hash(self):
        assert extract_deploy_hash("deploy 8f13a2 added new code") == "8f13a2"
        assert extract_deploy_hash("deploy-8f13a2 was bad") == "8f13a2"
        assert extract_deploy_hash("no deploy mentioned") is None

    def test_extract_endpoints(self):
        text = "POST /checkout and GET /orders/123"
        endpoints = extract_endpoints(text)
        assert "/checkout" in endpoints
        assert "/orders/123" in endpoints

    def test_classify_source_type(self):
        assert classify_source_type("service.py") == "code"
        assert classify_source_type("app.log") == "logs"
        assert classify_source_type("diff-abc.patch") == "deploy"
        assert classify_source_type("deploy-history.json") == "deploy"
        assert classify_source_type("incidents/outage.md") == "incident"
        assert classify_source_type("runbooks/guide.md") == "runbook"

    def test_classify_doc_type(self):
        assert classify_doc_type("service.py") == "code"
        assert classify_doc_type("errors.log") == "log"
        assert classify_doc_type("openapi.yaml") == "api_doc"
        assert classify_doc_type("incidents/report.md") == "incident"


# ── Security sanitizer ───────────────────────

class TestSanitizer:
    def test_redacts_api_key(self):
        text = "using key sk-abc123456789012345678901234567890"
        result = redact_secrets(text)
        assert "sk-abc" not in result
        assert "REDACTED" in result

    def test_redacts_bearer_token(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result = redact_secrets(text)
        assert "eyJ" not in result

    def test_redacts_password(self):
        text = "password=mysecretpassword123"
        result = redact_secrets(text)
        assert "mysecretpassword" not in result

    def test_preserves_normal_text(self):
        text = "Checkout latency spiked to 2500ms after deploy 8f13a2"
        result = redact_secrets(text)
        assert result == text
