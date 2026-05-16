from __future__ import annotations

from incidentops.investigation.schemas import InvestigationEntities


def resolve_scope(entities: InvestigationEntities) -> dict:
    filters = {}
    query_variants = []
    if entities.service_name:
        query_variants.append(entities.service_name)
    if entities.endpoint:
        filters["endpoint"] = entities.endpoint
        query_variants.append(entities.endpoint)
    if entities.deploy_hash:
        filters["deploy_hash"] = entities.deploy_hash
        query_variants.append(f"deploy {entities.deploy_hash}")
    if entities.symptom:
        query_variants.append(entities.symptom.replace("_", " "))

    default_window = None
    if entities.deploy_hash:
        default_window = {"relative_to": entities.deploy_hash, "before_minutes": 15, "after_minutes": 120}

    return {
        "filters": filters,
        "query_variants": query_variants,
        "time_window": entities.time_window or default_window,
    }
