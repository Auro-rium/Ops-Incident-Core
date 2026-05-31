from __future__ import annotations

from incidentops.ingestion.failure_taxonomy import failure_reason_counts, normalize_failure_code


def test_failure_taxonomy_maps_legacy_parser_codes():
    assert normalize_failure_code("parse_error") == "parser_exception"
    assert normalize_failure_code("no_chunks_parsed") == "empty"
    assert normalize_failure_code("too_many_chunks") == "chunk_limit_exceeded"
    assert normalize_failure_code("embedding_error") == "embedding_failed"


def test_failure_taxonomy_counts_public_reasons():
    counts = failure_reason_counts(["unsafe_path", "metadata_too_large", "parse_error", "parse_error"])
    assert counts == {"metadata_invalid": 2, "parser_exception": 2}
