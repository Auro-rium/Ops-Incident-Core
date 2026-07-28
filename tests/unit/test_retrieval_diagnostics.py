from __future__ import annotations

from types import SimpleNamespace

from incidentops.retrieval.evidence_packer import pack_evidence
from incidentops.retrieval.hybrid_search import _metadata_boost
from incidentops.retrieval.query_intent import classify_query_intent


def _chunk(
    path: str,
    *,
    chunk_type: str = "markdown_section",
    source_type: str = "runbook",
    text: str = "example text",
    start_line: int | None = 1,
    end_line: int | None = 5,
):
    return SimpleNamespace(
        id=f"{path}:{start_line}:{end_line}",
        chunk_type=chunk_type,
        service_name=None,
        endpoint=None,
        deploy_hash=None,
        section_title=None,
        start_line=start_line,
        end_line=end_line,
        timestamp_start=None,
        timestamp_end=None,
        text=text,
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


def test_pack_evidence_prefers_highest_score_and_dedupes():
    results = [
        {
            "chunk": _chunk(
                "README.md",
                chunk_type="markdown_section",
                source_type="runbook",
                text="overview" * 50,
            ),
            "fused_score": 0.42,
            "metadata_boost_reasons": ["readme_penalty"],
        },
        {
            "chunk": _chunk(
                "service/history/handler.go",
                chunk_type="go_function",
                source_type="code",
                text="func HistoryServiceLatencyProbe() {\n    return\n}\n",
                start_line=10,
                end_line=22,
            ),
            "fused_score": 0.91,
            "metadata_boost_reasons": ["intent_source:code", "code_path_match"],
        },
        {
            "chunk": _chunk(
                "service/history/handler.go",
                chunk_type="go_function",
                source_type="code",
                text="func HistoryServiceLatencyProbe() {\n    return\n}\n",
                start_line=10,
                end_line=22,
            ),
            "fused_score": 0.90,
            "metadata_boost_reasons": ["intent_source:code"],
        },
    ]

    packed = pack_evidence(results, max_evidence=5, max_chars_per_chunk=80, max_total_chars=120)

    assert len(packed) == 2
    assert packed[0]["document_path"] == "service/history/handler.go"
    assert packed[0]["chunk_type"] == "go_function"
    assert "code_path_match" in packed[0]["why_retrieved"]
    assert sum(len(item["text"]) for item in packed) <= 120


def test_pack_evidence_recognizes_source_aware_chunk_types():
    results = [
        {"chunk": _chunk("service/history/handler.go", chunk_type="go_struct", source_type="unknown_text"), "fused_score": 0.9},
        {"chunk": _chunk("api/history.yaml", chunk_type="openapi_endpoint", source_type="unknown_text"), "fused_score": 0.8},
        {"chunk": _chunk("logs/history.log", chunk_type="log_error_burst", source_type="unknown_text"), "fused_score": 0.7},
    ]

    packed = pack_evidence(results)

    assert [item["source_type"] for item in packed] == ["code", "api_doc", "logs"]
