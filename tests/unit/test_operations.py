from __future__ import annotations

from incidentops.operations.service import (
    RUN_EVALUATOR,
    RUN_LOGGING,
    RUN_OBSERVER,
    build_idempotency_key,
    sanitize_operational_payload,
)


def test_operational_run_idempotency_is_deterministic_and_typed():
    request = {"top_k": 8, "eval_run_id": "example"}

    assert build_idempotency_key(RUN_EVALUATOR, request) == build_idempotency_key(RUN_EVALUATOR, request)
    assert len({RUN_EVALUATOR, RUN_OBSERVER, RUN_LOGGING}) == 3


def test_operational_payload_redacts_raw_evidence_and_secrets():
    payload = sanitize_operational_payload(
        {
            "query_intent": "code_location",
            "content": "do not persist this",
            "token": "abc123",
            "nested": {"evidence": "raw chunk", "latency_ms": 14},
        }
    )

    assert payload["query_intent"] == "code_location"
    assert payload["content"] == "[REDACTED]"
    assert payload["token"] == "[REDACTED]"
    assert payload["nested"]["evidence"] == "[REDACTED]"
    assert payload["nested"]["latency_ms"] == 14
