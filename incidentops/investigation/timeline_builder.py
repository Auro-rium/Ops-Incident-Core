from __future__ import annotations

from datetime import datetime

from incidentops.investigation.schemas import TimelineEvent


def build_timeline(
    evidence: list[dict],
    log_findings: dict,
    deploy_findings: dict,
    previous_incidents: list[dict],
) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    for item in evidence:
        source_type = item.get("source_type")
        if source_type == "deploy":
            summary = (
                f"deploy evidence found for {item.get('deploy_hash')}"
                if item.get("deploy_hash")
                else "deploy evidence found"
            )
            events.append(
                TimelineEvent(
                    event_type="deploy",
                    summary=summary,
                    ts=item.get("timestamp_start"),
                    evidence_chunk_ids=[item["chunk_id"]],
                )
            )
        elif source_type == "logs" and item.get("timestamp_start"):
            summary = "log evidence found"
            text = item.get("text", "")
            if "timeout" in text.lower():
                summary = "timeout evidence appears in logs"
            elif "error" in text.lower():
                summary = "error evidence appears in logs"
            elif "ms" in text.lower():
                summary = "latency evidence appears in logs"
            events.append(
                TimelineEvent(
                    event_type="log_signal",
                    summary=summary,
                    ts=item.get("timestamp_start"),
                    evidence_chunk_ids=[item["chunk_id"]],
                )
            )
    if previous_incidents:
        top = previous_incidents[0]
        events.append(
            TimelineEvent(
                event_type="similar_incident",
                summary=f"similar previous incident found: {top['document_path']}",
                evidence_chunk_ids=[top["chunk_id"]],
            )
        )
    events.sort(key=lambda event: (event.ts is None, event.ts or datetime.max))
    return events
