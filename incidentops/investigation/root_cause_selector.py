from __future__ import annotations

from incidentops.investigation.schemas import Hypothesis

UNSUPPORTED_CLAIMS = (
    "database outage",
    "payment provider outage",
    "auth service failure",
)


def select_root_cause(hypotheses: list[Hypothesis], evidence: list[dict]) -> Hypothesis:
    if not hypotheses:
        return Hypothesis(
            hypothesis_id="weak_evidence",
            summary="Evidence is insufficient to identify a confident root cause.",
            score=0.0,
            confidence="low",
            evidence_chunk_ids=[],
            supporting_reasons=[],
        )
    best = hypotheses[0]
    summary = best.summary.lower()
    if any(claim in summary for claim in UNSUPPORTED_CLAIMS):
        return Hypothesis(
            hypothesis_id="weak_evidence",
            summary="Evidence is insufficient to identify a confident root cause.",
            score=0.0,
            confidence="low",
            evidence_chunk_ids=[],
            supporting_reasons=["top hypothesis contained an unsupported claim"],
        )
    if best.score < 0.7 or best.confidence == "low":
        best.summary = "Evidence is insufficient to identify a confident root cause."
        best.confidence = "low"
    return best
