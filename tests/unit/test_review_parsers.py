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


# ── Routing and classification ────────────────

from incidentops.collector.taxonomy import classify  # noqa: E402
from incidentops.ingestion.chunking.metadata import classify_doc_type, classify_source_type  # noqa: E402


def test_python_module_under_incidentops_package_uses_python_parser():
    content = "import os\n\n\ndef run():\n    return os.getcwd()\n"
    chunks, error = _parse_normalized_document(_doc("incidentops/agent.py", content, "code"), service_name=None)
    assert error is None
    assert {chunk.chunk_type for chunk in chunks} >= {"python_function"}
    assert all(chunk.source_type == "code" for chunk in chunks)


def test_runbook_mentioning_incident_in_name_is_not_parsed_as_incident():
    chunks, _ = _parse_normalized_document(
        _doc("docs/incident-response-guide.md", "# Guide\n## Steps\nDo things\n", "runbook"),
        service_name=None,
    )
    assert chunks and all(chunk.source_type != "incident" for chunk in chunks)


@pytest.mark.parametrize("path", ["incidents/2026-01-outage.md", "team/postmortems/db.txt"])
def test_incident_directory_markdown_uses_incident_parser(path):
    chunks, _ = _parse_normalized_document(_doc(path, "# Outage\n## Root Cause\nDisk\n", "runbook"), service_name=None)
    assert chunks and all(chunk.source_type == "incident" for chunk in chunks)


def test_explicit_incident_source_type_still_uses_incident_parser():
    chunks, _ = _parse_normalized_document(_doc("notes/outage.md", "# Outage\n## Timeline\nT0\n", "incident"), service_name=None)
    assert chunks and all(chunk.source_type == "incident" for chunk in chunks)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("incidentops/settings.yaml", "config"),
        ("incidentops/README.md", "runbook"),
        ("docs/postmortem-template.md", "runbook"),
        ("incidents/outage.md", "incident"),
        ("ops/postmortems/2026-db.txt", "incident"),
    ],
)
def test_classify_source_type_uses_incident_directory_segments(path, expected):
    assert classify_source_type(path) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("incidentops/settings.yaml", "config"),
        ("incidentops/README.md", "markdown"),
        ("incidents/outage.md", "incident"),
    ],
)
def test_classify_doc_type_uses_incident_directory_segments(path, expected):
    assert classify_doc_type(path) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/deploy-utils.py", "code"),
        ("tools/release-helper.go", "code"),
        ("incidentops/notes.md", "runbook"),
        ("incidentops/config.yaml", "config"),
        ("deploy/release.yaml", "deploy"),
        ("deploy-history.json", "deploy"),
        ("ops/deploy-2026-01.md", "deploy"),
        ("incidents/outage.md", "incident"),
        ("docs/redeploy-notes.md", "runbook"),
    ],
)
def test_collector_taxonomy_matches_on_path_segments(path, expected):
    assert classify(path) == expected
