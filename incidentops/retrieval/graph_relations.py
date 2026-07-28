"""Deterministic graph relations derived from canonical chunk metadata."""

from __future__ import annotations

from typing import Any


def build_chunk_relations(document, chunks: list) -> list[dict[str, Any]]:
    """Build only facts represented directly by an indexed chunk.

    Keys are stable identifiers, not natural-language claims. The graph is a
    retrieval aid for architecture and ownership questions; it is not an
    inferred call graph or an agent-authored knowledge base.
    """
    path = str(document.path or "")
    path_key = f"path:{path}"
    relations: list[dict[str, Any]] = []
    for chunk in chunks:
        metadata = chunk.metadata_json or {}
        relation_metadata = {
            "chunk_type": chunk.chunk_type,
            "document_path": path,
            "source_type": metadata.get("source_type", "unknown_text"),
        }
        relations.append(_relation(chunk, "contains", path_key, f"chunk:{chunk.id}", relation_metadata))
        if chunk.service_name:
            relations.append(_relation(chunk, "contains", f"service:{chunk.service_name}", path_key, relation_metadata))
        package_name = metadata.get("package_name")
        if isinstance(package_name, str) and package_name:
            relations.append(_relation(chunk, "contains", f"package:{package_name}", path_key, relation_metadata))
        symbol_name = metadata.get("symbol_name") or metadata.get("symbol")
        if isinstance(symbol_name, str) and symbol_name:
            relations.append(_relation(chunk, "defines", path_key, f"symbol:{symbol_name}", relation_metadata))
        if chunk.endpoint:
            relations.append(_relation(chunk, "implements", path_key, f"endpoint:{chunk.endpoint}", relation_metadata))
        if chunk.deploy_hash:
            relations.append(_relation(chunk, "changed_by", path_key, f"deploy:{chunk.deploy_hash}", relation_metadata))
    return relations


def _relation(chunk, relation_type: str, from_key: str, to_key: str, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": chunk.project_id,
        "evidence_chunk_id": chunk.id,
        "relation_type": relation_type,
        "from_key": from_key[:512],
        "to_key": to_key[:512],
        "metadata_json": metadata,
    }
