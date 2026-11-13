from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from incidentops.investigation.hypothesis_generator import generate_hypotheses
from incidentops.investigation.root_cause_selector import (
    INSUFFICIENT_EVIDENCE_SUMMARY,
    UNSUPPORTED_CLAIMS,
    reconcile_confidence,
    select_root_cause,
)
from incidentops.investigation.schemas import Hypothesis
from incidentops.investigation.service import _build_cited_answer, _missing_data


def _hypothesis(score: float = 0.72, summary: str = "a recent deploy may have changed behavior") -> Hypothesis:
    return Hypothesis(
        hypothesis_id="h1",
        summary=summary,
        score=score,
        confidence="medium",
        evidence_chunk_ids=["c1"],
        supporting_reasons=["r"],
    )


def _all_generated_hypotheses() -> list[Hypothesis]:
    return generate_hypotheses(
        {},
        [{"chunk_id": "c1"}],
        {"slow_requests": 1, "errors": 1, "timeouts": 1, "services": []},
        {"diff_count": 1},
        [{"chunk_id": "c2"}],
    )


def test_selector_does_not_mutate_low_scoring_hypothesis():
    hypotheses = [_hypothesis(score=0.5, summary="timeouts in a downstream dependency")]
    before = copy.deepcopy(hypotheses)

    selected = select_root_cause(hypotheses, [{"source_type": "logs"}])

    assert hypotheses == before
    assert selected.summary == INSUFFICIENT_EVIDENCE_SUMMARY
    assert selected.confidence == "low"


def test_selector_returns_a_copy_of_the_winning_hypothesis():
    hypotheses = [_hypothesis()]

    selected = select_root_cause(hypotheses, [{"source_type": "deploy"}])

    assert selected == hypotheses[0]
    assert selected is not hypotheses[0]


def test_every_unsupported_claim_matches_a_generated_summary():
    summaries = [hypothesis.summary.lower() for hypothesis in _all_generated_hypotheses()]

    for claim in UNSUPPORTED_CLAIMS:
        assert any(claim in summary for summary in summaries), claim


def test_claim_without_backing_evidence_is_rejected():
    selected = select_root_cause([_hypothesis()], [{"source_type": "code"}])

    assert selected.hypothesis_id == "weak_evidence"
    assert selected.confidence == "low"
    assert selected.evidence_chunk_ids == []


def test_claim_with_backing_evidence_is_kept():
    selected = select_root_cause([_hypothesis()], [{"source_type": "deploy"}])

    assert selected.hypothesis_id == "h1"


def test_reconcile_low_score_lowers_hypothesis_and_summary_without_mutating():
    selected = _hypothesis()

    reconciled = reconcile_confidence(selected, "low")

    assert (reconciled.confidence, reconciled.summary) == ("low", INSUFFICIENT_EVIDENCE_SUMMARY)
    assert selected.confidence == "medium"


def test_reconcile_keeps_selector_rejection_low_even_when_scorer_is_higher():
    weak = select_root_cause([_hypothesis(score=0.5)], [{"source_type": "deploy"}])

    assert reconcile_confidence(weak, "medium").confidence == "low"


@pytest.mark.parametrize("scored", ["medium", "high"])
def test_reconcile_applies_scored_confidence_to_a_supported_hypothesis(scored):
    reconciled = reconcile_confidence(_hypothesis(), scored)

    assert reconciled.confidence == scored
    assert reconciled.summary == _hypothesis().summary


def _answer(confidence: str, missing_data: list[str]) -> str:
    return _build_cited_answer(
        [{"citation": {"label": "[1]"}, "document_path": "a.py"}],
        replace(_hypothesis(), confidence=confidence),
        True,
        missing_data,
    )


def test_high_confidence_answer_omits_not_confident_message_despite_missing_data():
    assert "does not support" not in _answer("high", ["no previous incident reports"])


@pytest.mark.parametrize("confidence", ["low", "medium"])
def test_below_high_answer_states_not_confident(confidence):
    assert "does not support a confident" in _answer(confidence, [])


def _findings(timestamped: int = 0) -> dict:
    return {"timestamped_logs": timestamped}


def test_missing_deploy_evidence_is_reported_once():
    missing = _missing_data([], _findings(), {"diff_count": 0}, [])

    assert [entry for entry in missing if "deploy" in entry or "diff" in entry] == [
        "deployment history or code diff not found"
    ]


def test_missing_timestamps_only_reported_when_logs_exist():
    no_logs = _missing_data([{"source_type": "code"}], _findings(), {"diff_count": 1}, [])
    untimed = _missing_data([{"source_type": "logs"}], _findings(), {"diff_count": 1}, [])
    timed = _missing_data([{"source_type": "logs"}], _findings(timestamped=1), {"diff_count": 1}, [])

    assert "logs missing timestamps" not in no_logs
    assert "no runtime logs found" in no_logs
    assert "logs missing timestamps" in untimed
    assert "no runtime logs found" not in untimed
    assert "logs missing timestamps" not in timed
