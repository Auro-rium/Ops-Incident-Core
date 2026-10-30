"""Regression tests for ingestion parser, chunking and routing review findings."""

from __future__ import annotations

import signal

import pytest

from incidentops.ingestion.chunking.chunker import split_text_by_tokens


@pytest.fixture
def time_limit():
    def handler(signum, frame):  # pragma: no cover - only fires on regression
        raise TimeoutError("operation did not terminate")

    previous = signal.signal(signal.SIGALRM, handler)
    signal.alarm(2)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.parametrize("overlap_tokens", [50, 100, 500])
def test_split_text_terminates_when_overlap_reaches_window(time_limit, overlap_tokens):
    text = " ".join(f"w{i}" for i in range(400))
    chunks = split_text_by_tokens(text, max_tokens=50, overlap_tokens=overlap_tokens)
    assert 1 < len(chunks) <= 400
    assert chunks[-1].split()[-1] == "w399"


def test_split_text_does_not_emit_redundant_tail_chunks():
    text = " ".join(f"w{i}" for i in range(100))
    chunks = split_text_by_tokens(text, max_tokens=65, overlap_tokens=13)
    assert all(chunk.split()[-1] != "w99" for chunk in chunks[:-1])


def test_split_text_handles_tiny_max_tokens(time_limit):
    chunks = split_text_by_tokens("a b c d", max_tokens=0, overlap_tokens=0)
    assert " ".join(chunks).split() == ["a", "b", "c", "d"]


# ── Code parsers ──────────────────────────────

from incidentops.ingestion.indexer import _parse_normalized_document  # noqa: E402
from incidentops.ingestion.normalized import NormalizedDocument  # noqa: E402
from incidentops.ingestion.parsers.code_parser import parse_java, parse_jsts  # noqa: E402


def _doc(path: str, content: str, source_type: str | None = None, **metadata) -> NormalizedDocument:
    return NormalizedDocument(
        external_id=path,
        path=path,
        source_type=source_type or "",
        content=content,
        content_hash="0" * 64,
        metadata=metadata,
        size_bytes=len(content),
    )


@pytest.mark.parametrize("language", ["jsx", "tsx"])
def test_jsx_tsx_functions_parse_without_index_error(language):
    content = "import x from 'y'\n\nfunction App() {\n  return null\n}\n"
    chunks = parse_jsts(content, f"web/App.{language}", language=language)
    assert any(chunk.metadata.get("symbol_name") == "App" for chunk in chunks)


@pytest.mark.parametrize("suffix", [".jsx", ".tsx"])
def test_indexer_routes_jsx_tsx_to_js_parser(suffix):
    content = "function App() {\n  return null\n}\nconst Other = () => 1\n"
    chunks, error = _parse_normalized_document(_doc(f"web/App{suffix}", content, "code"), service_name=None)
    assert error is None
    assert {chunk.metadata.get("symbol_name") for chunk in chunks} >= {"App"}


def test_java_regex_ignores_control_flow_statements():
    content = (
        "package demo;\n"
        "public class Service {\n"
        "  public int run(int x) {\n"
        "    if (x > 0) {\n"
        "      return compute(x);\n"
        "    } else if (x < 0) {\n"
        "      throw new IllegalStateException(\"neg\");\n"
        "    }\n"
        "    for (int i = 0; i < x; i++) {}\n"
        "    while (x > 0) {}\n"
        "    switch (x) {}\n"
        "    return new Result(x);\n"
        "  }\n"
        "  private String name() {\n"
        "    return \"n\";\n"
        "  }\n"
        "}\n"
    )
    symbols = [chunk.metadata.get("symbol_name") for chunk in parse_java(content, "Service.java")]
    symbols = [symbol for symbol in symbols if symbol]
    assert symbols == ["Service", "run", "name"]
