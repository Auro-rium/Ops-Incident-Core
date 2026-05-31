from __future__ import annotations

from types import SimpleNamespace

from incidentops.retrieval.hybrid_search import _metadata_boost
from incidentops.retrieval.query_intent import classify_query_intent


def _chunk(path: str, *, chunk_type: str = "markdown_section", source_type: str = "runbook"):
    return SimpleNamespace(
        chunk_type=chunk_type,
        service_name=None,
        endpoint=None,
        deploy_hash=None,
        metadata_json={"source_type": source_type, "document_path": path},
        document=SimpleNamespace(path=path),
    )


def test_code_location_boosts_code_and_penalizes_readme():
    intent = classify_query_intent("Where is history service implemented?")
    query_info = {"deploy_hashes": [], "services": ["history"], "endpoints": [], "query_terms": intent.query_terms}

    code_boost, code_reasons = _metadata_boost(
        _chunk("service/history/handler.go", chunk_type="function", source_type="code"),
        query_info,
        intent,
    )
    readme_boost, readme_reasons = _metadata_boost(_chunk("README.md"), query_info, intent)

    assert "intent_source:code" in code_reasons
    assert "code_path_match" in code_reasons
    assert "readme_penalty" in readme_reasons
    assert code_boost > readme_boost


def test_code_location_with_latency_symbol_does_not_become_runtime_query():
    intent = classify_query_intent("Where is HistoryServiceLatencyProbe implemented?")

    assert intent.intent == "code_location"
    assert "go_function" in intent.preferred_chunk_types


def test_runtime_query_warns_when_runtime_evidence_missing():
    intent = classify_query_intent("Why did workflow latency spike?")
    boost, reasons = _metadata_boost(
        _chunk("docs/architecture.md", chunk_type="markdown_section", source_type="runbook"),
        {"deploy_hashes": [], "services": [], "endpoints": [], "query_terms": intent.query_terms},
        intent,
    )
    assert intent.intent == "runtime_incident"
    assert "intent_source:runbook" in reasons
    assert boost > 0
