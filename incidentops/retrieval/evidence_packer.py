"""
Evidence packer.
"""

from __future__ import annotations

from incidentops.security.output_sanitizer import sanitize_output
from incidentops.security.prompt_injection import inspect_untrusted_text
from incidentops.security.secret_redaction import redact_secrets
from incidentops.retrieval.source_types import chunk_source_type

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
DEFAULT_PREVIEW_CHARS = 300
# Evidence flagged for instruction-like text stays visible (flag-only policy)
# but drops this many positions so clean evidence of similar rank leads.
PROMPT_INJECTION_RANK_PENALTY = 1


def _source_type_from_chunk(chunk) -> str:
    return chunk_source_type(chunk)


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
            "score": _result_score(r),
            "reranked": r.get("rerank_score") is not None,
            "vector_score": r.get("vector_score"),
            "lexical_score": r.get("lexical_score"),
            "injection_flags": inspection,
            "prompt_injection_flagged": bool(inspection["is_suspicious"]),
            "why_retrieved": list(r.get("metadata_boost_reasons") or []),
        }
        evidence.append(item)
        total_chars += len(text)
        if len(evidence) >= max_evidence:
            break
    # A partial rerank yields a reranked head and a fused-score tail. The two
    # score scales are not comparable, so order by tier first, then score.
    evidence.sort(
        key=lambda e: (
            not e["reranked"],
            -e["score"],
            SOURCE_PRIORITY.get(e["source_type"], 99),
            e["document_path"],
        )
    )
    return _demote_flagged(evidence)[:max_evidence]


def evidence_preview(item: dict, max_chars: int = DEFAULT_PREVIEW_CHARS) -> str:
    """Sanitized, bounded preview of evidence text for API responses.

    The full text is sanitized before truncation so a cut cannot leave an
    unterminated script tag that the sanitizer no longer recognizes.
    """
    return sanitize_output(item.get("text") or "")[:max_chars]


def _result_score(result: dict) -> float:
    rerank_score = result.get("rerank_score")
    if rerank_score is not None:
        return float(rerank_score)
    return float(result.get("fused_score", 0.0) or 0.0)


def _demote_flagged(evidence: list[dict]) -> list[dict]:
    ranked = list(evidence)
    for item in [entry for entry in evidence if entry["prompt_injection_flagged"]]:
        index = ranked.index(item)
        target = min(index + PROMPT_INJECTION_RANK_PENALTY, len(ranked) - 1)
        if target != index:
            ranked.insert(target, ranked.pop(index))
    return ranked


def _compact_text(text: str, *, max_chars: int) -> str:
    cleaned = "\n".join(line.rstrip() for line in text.strip().splitlines())
    if len(cleaned) <= max_chars:
        return cleaned
    truncated = cleaned[: max(max_chars - 1, 0)].rstrip()
    return f"{truncated}..." if truncated else ""
