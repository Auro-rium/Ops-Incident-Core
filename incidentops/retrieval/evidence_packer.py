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

DEFAULT_MAX_CHARS_PER_CHUNK = 1600
DEFAULT_MAX_TOTAL_CHARS = 10000


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


def pack_evidence(
    results: list[dict],
    max_evidence: int = 12,
    *,
    max_chars_per_chunk: int = DEFAULT_MAX_CHARS_PER_CHUNK,
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
) -> list[dict]:
    evidence = []
    seen_signatures: set[tuple[str, int | None, int | None]] = set()
    total_chars = 0
    for r in results:
        chunk = r["chunk"]
        source_type = _source_type_from_chunk(chunk)
        doc = getattr(chunk, "document", None)
        doc_path = doc.path if doc else ""
        signature = (doc_path, chunk.start_line, chunk.end_line)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        text = _compact_text(redact_secrets(chunk.text), max_chars=max_chars_per_chunk)
        if not text:
            continue
        remaining = max_total_chars - total_chars
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = _compact_text(text, max_chars=remaining)
        if not text:
            break
        inspection = inspect_untrusted_text(text)
        item = {
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
            "why_retrieved": list(r.get("metadata_boost_reasons") or []),
        }
        evidence.append(item)
        total_chars += len(text)
        if len(evidence) >= max_evidence:
            break
    evidence.sort(key=lambda e: (-e["score"], SOURCE_PRIORITY.get(e["source_type"], 99), e["document_path"]))
    return evidence[:max_evidence]


def _compact_text(text: str, *, max_chars: int) -> str:
    cleaned = "\n".join(line.rstrip() for line in text.strip().splitlines())
    if len(cleaned) <= max_chars:
        return cleaned
    truncated = cleaned[: max(max_chars - 1, 0)].rstrip()
    return f"{truncated}..." if truncated else ""
