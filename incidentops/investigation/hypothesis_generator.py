from __future__ import annotations

from incidentops.investigation.schemas import Hypothesis


def generate_hypotheses(
    entities,
    evidence: list[dict],
    log_findings: dict,
    deploy_findings: dict,
    previous_incidents: list[dict],
) -> list[Hypothesis]:
    hypotheses: list[Hypothesis] = []
    evidence_chunk_ids = [item["chunk_id"] for item in evidence[:6]]

    if log_findings["slow_requests"] and deploy_findings["diff_count"]:
        hypotheses.append(
            Hypothesis(
                hypothesis_id="deploy_related_behavior_change",
                summary="a recent deploy may have changed request-path behavior and coincides with the observed symptom",
                score=0.72,
                confidence="medium",
                evidence_chunk_ids=evidence_chunk_ids,
                supporting_reasons=[
                    "logs show the reported symptom",
                    "deployment evidence exists near the incident context",
                ],
            )
        )
    if log_findings["timeouts"]:
        hypotheses.append(
            Hypothesis(
                hypothesis_id="timeout_amplification",
                summary="timeouts in a downstream dependency or critical path may be amplifying incident impact",
                score=0.66,
                confidence="medium",
                evidence_chunk_ids=evidence_chunk_ids,
                supporting_reasons=["logs contain timeout evidence"],
            )
        )
    if log_findings["errors"]:
        hypotheses.append(
            Hypothesis(
                hypothesis_id="error_path_regression",
                summary="error-path behavior appears elevated and may indicate a regression or failing dependency",
                score=0.62,
                confidence="medium",
                evidence_chunk_ids=evidence_chunk_ids,
                supporting_reasons=["logs contain elevated error markers"],
            )
        )
    if previous_incidents:
        hypotheses.append(
            Hypothesis(
                hypothesis_id="previous_incident_pattern",
                summary="a previous incident appears similar and may provide a matching operational pattern",
                score=0.58,
                confidence="medium",
                evidence_chunk_ids=[previous_incidents[0]["chunk_id"]],
                supporting_reasons=["previous incident evidence was found"],
            )
        )
    if not hypotheses:
        hypotheses.append(
            Hypothesis(
                hypothesis_id="weak_evidence",
                summary="available evidence is insufficient to support a confident root-cause determination",
                score=0.25,
                confidence="low",
                evidence_chunk_ids=evidence_chunk_ids,
                supporting_reasons=["retrieval returned weak or sparse evidence"],
            )
        )
    return sorted(hypotheses, key=lambda item: item.score, reverse=True)
