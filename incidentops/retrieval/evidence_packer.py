"""
Evidence packer.
"""

from __future__ import annotations

from incidentops.security.prompt_injection import inspect_untrusted_text
from incidentops.security.secret_redaction import redact_secrets

SOURCE_PRIORITY = {
    "deploy": 0,
    "logs": 1,
    "code": 2,
    "incident": 3,
    "config": 4,
    "api_doc": 5,
    "runbook": 6,
}


def _source_type_from_chunk(chunk) -> str:
    metadata = chunk.metadata_json or {}
    raw_source_type = metadata.get("source_type")
    if raw_source_type in {"deploy", "logs", "code", "incident", "runbook", "api_doc", "config"}:
        return raw_source_type
    if raw_source_type == "incident_report":
        return "incident"
    if raw_source_type == "deploy_history":
        return "deploy"
    ct = chunk.chunk_type or ""
    if ct == "deploy_diff":
        return "deploy"
    if ct == "log_window":
        return "logs"
    if ct in {"function", "class", "module", "code_file"}:
        return "code"
    if ct == "incident_section":
        return "incident"
    if ct in ("api_endpoint", "proto_service", "proto_message", "proto_preamble"):
        return "api_doc"
    if ct == "config_section":
        return "config"
    if ct in ("markdown_section",):
        doc_path = metadata.get("document_path", "")
        path = getattr(chunk, "document_path", doc_path) or ""
        lower_path = path.lower()
        if "runbook" in path.lower():
            return "runbook"
        if "api" in lower_path or "openapi" in lower_path or "swagger" in lower_path:
            return "api_doc"
        if lower_path.endswith((".yaml", ".yml", ".json", ".toml", ".ini")):
            return "config"
        return "runbook"
    return "runbook"


def pack_evidence(results: list[dict], max_evidence: int = 12) -> list[dict]:
    evidence = []
    for r in results:
        chunk = r["chunk"]
        source_type = _source_type_from_chunk(chunk)
        doc = getattr(chunk, "document", None)
        doc_path = doc.path if doc else ""
        text = redact_secrets(chunk.text)
        inspection = inspect_untrusted_text(text)
        evidence.append(
            {
                "chunk_id": str(chunk.id),
                "source_type": source_type,
                "chunk_type": chunk.chunk_type,
                "document_path": doc_path,
                "service_name": chunk.service_name,
                "deploy_hash": chunk.deploy_hash,
                "endpoint": chunk.endpoint,
                "section_title": chunk.section_title,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "timestamp_start": chunk.timestamp_start,
                "timestamp_end": chunk.timestamp_end,
                "text": text,
                "score": r.get("rerank_score") or r.get("fused_score", 0.0),
                "vector_score": r.get("vector_score"),
                "lexical_score": r.get("lexical_score"),
                "injection_flags": inspection,
            }
        )
    evidence.sort(key=lambda e: (SOURCE_PRIORITY.get(e["source_type"], 99), -e["score"]))
    return evidence[:max_evidence]
