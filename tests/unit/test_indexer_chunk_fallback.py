from __future__ import annotations

from types import SimpleNamespace

from incidentops.ingestion.indexer import _build_bounded_fallback_chunks
from incidentops.ingestion.normalized import NormalizedDocument


def test_bounded_fallback_chunks_keep_large_go_document_indexable():
    content = "\n".join(f"func handlerLine{i}() string {{ return \"{i}\" }}" for i in range(1, 1201))
    normalized = NormalizedDocument(
        external_id="service/history/huge.go",
        path="service/history/huge.go",
        title="huge.go",
        content=content,
        source_type="code",
        content_hash="abc123",
        size_bytes=len(content.encode("utf-8")),
        metadata={"language": "go", "package_name": "history"},
    )
    settings = SimpleNamespace(max_chunks_per_document=8, max_chunk_tokens=64)

    chunks = _build_bounded_fallback_chunks(normalized, service_name="history", settings=settings)

    assert 1 <= len(chunks) <= settings.max_chunks_per_document
    assert all(chunk.chunk_type == "go_fallback" for chunk in chunks)
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line is not None
    assert all(chunk.metadata.get("fallback_reason") == "chunk_limit_guard" for chunk in chunks)
