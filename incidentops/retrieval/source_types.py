"""Single source-type normalizer shared by retrieval, answers, and investigation."""

from __future__ import annotations

_SOURCE_TYPE_ALIASES = {
    "deploy_history": "deploy",
    "patch": "deploy",
    "incident_report": "incident",
    "markdown": "runbook",
    "unknown": "unknown_text",
    "deploys": "deploy",
    "log": "logs",
    "incidents": "incident",
    "postmortem": "incident",
    "api": "api_doc",
}

KNOWN_SOURCE_TYPES = frozenset({"deploy", "logs", "code", "incident", "runbook", "api_doc", "config"})


def canonical_source_type(value: str | None) -> str:
    if not value:
        return "unknown_text"
    return _SOURCE_TYPE_ALIASES.get(value, value)


def chunk_source_type(chunk) -> str:
    """Canonical source type for a chunk (deploy, logs, code, incident, runbook, api_doc, config, unknown_text)."""
    metadata = chunk.metadata_json or {}
    raw = metadata.get("source_type")
    if isinstance(raw, str) and canonical_source_type(raw) in KNOWN_SOURCE_TYPES:
        return canonical_source_type(raw)
    chunk_type = (chunk.chunk_type or "").lower()
    if chunk_type in {"deploy_diff", "deploy_commit"}:
        return "deploy"
    if chunk_type in {"log_window", "error_cluster", "log_time_window", "log_error_burst"}:
        return "logs"
    if chunk_type in {
        "function",
        "class",
        "module",
        "code_file",
        "go_fallback",
        "python_fallback",
        "ts_fallback",
        "js_fallback",
        "java_fallback",
        "go_function",
        "go_method",
        "go_type",
        "go_struct",
        "go_interface",
        "go_module",
        "python_function",
        "python_class",
        "python_module",
        "ts_function",
        "ts_class",
        "ts_interface",
        "java_method",
        "java_class",
        "java_interface",
    }:
        return "code"
    if chunk_type in {"incident_section", "incident_symptom", "incident_timeline", "incident_root_cause", "incident_action"}:
        return "incident"
    if chunk_type in {"api_endpoint", "openapi_endpoint", "openapi_schema", "proto_service", "proto_rpc", "proto_message", "proto_enum", "proto_preamble", "proto_fallback"}:
        return "api_doc"
    if chunk_type in {"config_section", "config_service_block", "env_var_block", "dependency_block", "config_fallback"}:
        return "config"
    if chunk_type in {"markdown_section", "markdown_heading_section", "markdown_procedure", "markdown_table", "markdown_faq"}:
        doc_path = getattr(getattr(chunk, "document", None), "path", "") or metadata.get("document_path", "")
        lower_path = str(doc_path).lower()
        if "api" in lower_path or "openapi" in lower_path or "swagger" in lower_path:
            return "api_doc"
        return "runbook"
    return "unknown_text"
