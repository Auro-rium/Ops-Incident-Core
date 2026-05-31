from __future__ import annotations

from incidentops.retrieval.query_intent import classify_query_intent, investigate_supported


def test_classify_code_location_query():
    intent = classify_query_intent("Where is the history service implemented?")
    assert intent.intent == "code_location"
    assert "code" in intent.preferred_source_types


def test_classify_config_query():
    intent = classify_query_intent("Which config defines the database connection?")
    assert intent.intent == "config_lookup"
    assert "config" in intent.preferred_source_types


def test_classify_architecture_query():
    intent = classify_query_intent("Which documentation files reference service architecture?")
    assert intent.intent == "architecture"


def test_classify_root_cause_query():
    intent = classify_query_intent("Why did checkout latency spike after the last deploy?")
    assert intent.intent == "runtime_incident"


def test_investigate_supported_rejects_repo_only_code_lookup():
    intent = classify_query_intent("Where is the checkout handler implemented?")
    supported, reasons = investigate_supported(intent, {"code", "runbook"})
    assert supported is False
    assert reasons


def test_investigate_supported_requires_logs_for_runtime():
    intent = classify_query_intent("What timeout errors happened at runtime?")
    supported, reasons = investigate_supported(intent, {"code", "runbook"})
    assert supported is False
    assert "requires logs" in reasons[0]


def test_investigate_supported_allows_root_cause_when_logs_exist():
    intent = classify_query_intent("Why did checkout latency spike?")
    supported, reasons = investigate_supported(intent, {"logs", "code"})
    assert supported is True
    assert reasons == []
