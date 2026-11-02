from __future__ import annotations

from dataclasses import replace

from incidentops.investigation.schemas import Hypothesis

INSUFFICIENT_EVIDENCE_SUMMARY = "Evidence is insufficient to identify a confident root cause."

# Causal phrases emitted by hypothesis_generator, mapped to the evidence source
# type that must be present before the claim may be presented as a root cause.
UNSUPPORTED_CLAIMS = {
    "recent deploy": "deploy",
    "timeouts in a downstream": "logs",
    "error-path": "logs",
    "previous incident": "incident",
}


def _weak_evidence(supporting_reasons: list[str]) -> Hypothesis:
    return Hypothesis(
        hypothesis_id="weak_evidence",
        summary=INSUFFICIENT_EVIDENCE_SUMMARY,
        score=0.0,
        confidence="low",
        evidence_chunk_ids=[],
        supporting_reasons=supporting_reasons,
    )


def select_root_cause(hypotheses: list[Hypothesis], evidence: list[dict]) -> Hypothesis:
    """Pick the leading hypothesis, returning a copy so ``hypotheses`` stays as generated."""
    if not hypotheses:
        return _weak_evidence([])
    best = hypotheses[0]
    summary = best.summary.lower()
    source_types = {item.get("source_type") for item in evidence}
    if any(claim in summary and required not in source_types for claim, required in UNSUPPORTED_CLAIMS.items()):
        return _weak_evidence(["top hypothesis contained an unsupported claim"])
    if best.score < 0.7 or best.confidence == "low":
        return replace(best, summary=INSUFFICIENT_EVIDENCE_SUMMARY, confidence="low")
    return replace(best)


def reconcile_confidence(selected: Hypothesis, scored_confidence: str) -> Hypothesis:
    """Apply the evidence-mix confidence while keeping confidence and summary consistent.

    A hypothesis the selector rejected stays low whatever the evidence mix
    scores, and a low score never keeps a causal summary. Generated hypotheses
    top out at medium, so the score is otherwise applied as is; taking the lower
    of the two would make high confidence unreachable.
    """
    if selected.confidence == "low" or scored_confidence == "low":
        return replace(selected, summary=INSUFFICIENT_EVIDENCE_SUMMARY, confidence="low")
    return replace(selected, confidence=scored_confidence)
