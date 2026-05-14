from __future__ import annotations


def find_previous_incidents(evidence: list[dict], entities: dict | None = None) -> list[dict]:
    matches = []
    service = (entities or {}).get("service_name")
    symptom = (entities or {}).get("symptom")
    for item in evidence:
        if item.get("source_type") != "incident":
            continue
        text = item.get("text", "").lower()
        score = 0.35
        if service and service in text:
            score += 0.25
        if symptom and symptom in text:
            score += 0.2
        if "root cause" in text or "timeline" in text:
            score += 0.1
        matches.append(
            {
                "chunk_id": item["chunk_id"],
                "document_path": item.get("document_path", ""),
                "score": min(score, 1.0),
                "summary": item.get("text", "")[:220],
            }
        )
    return sorted(matches, key=lambda match: match["score"], reverse=True)
