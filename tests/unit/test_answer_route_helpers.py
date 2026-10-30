from __future__ import annotations

from types import SimpleNamespace

from apps.api.routes.answer import (
    _build_direct_answer,
    _is_direct_lookup_candidate,
    _llm_answer_body,
    _raw_source_types,
)
from incidentops.investigation.service import _raw_source_types as investigation_raw_source_types
from incidentops.llm.prompts import SYSTEM_PROMPT


def _chunk(source_type: str, chunk_type: str = "markdown_section", path: str = "docs/x.md"):
    return SimpleNamespace(chunk_type=chunk_type, metadata_json={"source_type": source_type}, document=SimpleNamespace(path=path))


def _evidence(count: int = 3, scores: list[float] | None = None) -> list[dict]:
    scores = scores or [0.9 - 0.1 * index for index in range(count)]
    return [
        {
            "chunk_id": f"c{index}",
            "document_path": f"svc/file{index}.go",
            "service_name": None,
            "text": f"func Handler{index}() {{}}\n" * 40,
            "score": scores[index],
            "citation": {"label": f"[{index + 1}]", "lines": "1-5", "path": f"svc/file{index}.go"},
        }
        for index in range(count)
    ]


def test_raw_source_types_normalize_aliases():
    results = [{"chunk": _chunk("deploy_history", "deploy_commit")}, {"chunk": _chunk("log", "log_window")}]

    assert _raw_source_types(results) == {"deploy", "logs"}
    assert investigation_raw_source_types(results) == {"deploy", "logs"}


def test_direct_lookup_accepts_java_method_chunks():
    result = {"chunk": _chunk("unknown_text", "java_method", "generated/OrderService.txt")}

    assert _is_direct_lookup_candidate("code_location", result) is True


def test_non_json_llm_reply_returns_warning():
    body, warning = _llm_answer_body({"raw_response": "not json", "_meta": {}}, _evidence())
    assert body is None
    assert warning

    body, warning = _llm_answer_body(None, _evidence())
    assert body is None
    assert warning


def test_malformed_llm_fields_return_warning_not_error():
    body, warning = _llm_answer_body(
        {"summary": "s", "likely_root_cause": "r", "confidence": "high", "affected_services": "checkout"},
        _evidence(),
    )

    assert body is None
    assert warning


def test_llm_answer_cites_only_citations_used_and_validates_confidence():
    body, warning = _llm_answer_body(
        {
            "summary": "s",
            "likely_root_cause": "r",
            "answer_text": "The handler is in [2].",
            "confidence": "very sure",
            "affected_services": ["checkout"],
            "citations_used": ["[2]", "3", "[9]"],
        },
        _evidence(),
    )

    assert warning is None
    assert [citation.label for citation in body.citations] == ["[2]", "[3]"]
    assert body.confidence == "low"
    assert body.answer_text == "The handler is in [2]."


def test_prompt_schema_includes_answer_text():
    assert '"answer_text"' in SYSTEM_PROMPT


def test_direct_answer_uses_settings_limits_and_score_gap_confidence():
    settings = SimpleNamespace(direct_answer_max_chars_per_chunk=50, direct_answer_max_total_chars=80)

    close = _build_direct_answer("code_location", _evidence(2, [0.80, 0.75]), settings)
    decisive = _build_direct_answer("code_location", _evidence(2, [0.95, 0.40]), settings)

    assert len(close.answer_text) <= 80
    assert close.confidence == "medium"
    assert decisive.confidence == "high"
