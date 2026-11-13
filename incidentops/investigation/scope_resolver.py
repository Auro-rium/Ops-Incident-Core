from __future__ import annotations

from typing import Any

from incidentops.investigation.schemas import InvestigationEntities


def resolve_scope(entities: InvestigationEntities, explicit_filters: dict[str, Any] | None = None) -> dict:
    """Build retrieval filters and query variants for an investigation.

    A deploy hash extracted from free text only boosts matching evidence (via
    hybrid search query analysis); it becomes a hard filter only when the
    caller passed ``deploy_hash`` explicitly.
    """
    filters: dict[str, Any] = {}
    query_variants = []
    if entities.service_name:
        query_variants.append(entities.service_name)
    if entities.endpoint:
        filters["endpoint"] = entities.endpoint
        query_variants.append(entities.endpoint)
    if entities.deploy_hash:
        query_variants.append(f"deploy {entities.deploy_hash}")
    if entities.symptom:
        query_variants.append(entities.symptom.replace("_", " "))
    if explicit_filters:
        filters.update({key: value for key, value in explicit_filters.items() if value})

    deploy_hash = filters.get("deploy_hash") or entities.deploy_hash
    default_window = None
    if deploy_hash:
        default_window = {"relative_to": deploy_hash, "before_minutes": 15, "after_minutes": 120}

    return {
        "filters": filters,
        "query_variants": query_variants,
        "time_window": entities.time_window or default_window,
    }
