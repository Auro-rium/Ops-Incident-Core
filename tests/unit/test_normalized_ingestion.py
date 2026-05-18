from __future__ import annotations

import hashlib

from incidentops.config.settings import Settings
from incidentops.ingestion.normalized import (
    DocumentBatchError,
    NormalizedDocument,
    normalize_source_type,
    validate_normalized_document,
)


def _doc(**overrides) -> NormalizedDocument:
    content = overrides.pop("content", "2026-05-05T10:00:00Z INFO api healthy")
    defaults = {
        "external_id": "logs/api.log",
        "path": "logs/api.log",
        "source_type": "logs",
        "content": content,
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "metadata": {"service_name": "api"},
        "size_bytes": len(content),
        "modified_at": None,
    }
    defaults.update(overrides)
    return NormalizedDocument(**defaults)


def test_normalized_document_rejects_missing_external_id():
    result = validate_normalized_document(_doc(external_id=""), Settings())
    assert isinstance(result, DocumentBatchError)
    assert result.code == "invalid_external_id"


def test_normalized_document_rejects_path_traversal_and_absolute_paths():
    traversal = validate_normalized_document(_doc(path="../secrets.log"), Settings())
    absolute = validate_normalized_document(_doc(path="/var/log/app.log"), Settings())
    assert isinstance(traversal, DocumentBatchError)
    assert traversal.code == "unsafe_path"
    assert isinstance(absolute, DocumentBatchError)
    assert absolute.code == "unsafe_path"


def test_normalized_document_rejects_oversized_metadata():
    settings = Settings(max_metadata_bytes=16)
    result = validate_normalized_document(_doc(metadata={"description": "x" * 100}), settings)
    assert isinstance(result, DocumentBatchError)
    assert result.code == "metadata_too_large"


def test_normalized_document_treats_empty_content_as_invalid_without_content_echo():
    result = validate_normalized_document(_doc(content="   ", content_hash=hashlib.sha256(b"   ").hexdigest()), Settings())
    assert isinstance(result, DocumentBatchError)
    assert result.code == "empty_content"
    assert "   " not in result.error


def test_unsupported_source_type_maps_safely():
    assert normalize_source_type("strange-export", "notes.txt") == "runbook"
    assert normalize_source_type("strange-export", "config/app.conf") == "unknown_text"
    assert normalize_source_type("json", "settings.json") == "config"
    assert normalize_source_type("deploy_history", "deploys/deploy-history.json") == "deploy"
    assert normalize_source_type("incident_report", "incidents/incident-001.md") == "incident"
