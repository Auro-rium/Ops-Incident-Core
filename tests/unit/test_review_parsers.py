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


# ── Python parser ─────────────────────────────

from incidentops.ingestion.parsers.code_parser import parse_python  # noqa: E402

PYTHON_MODULE = (
    "import os\n"                      # 1
    "\n"                               # 2
    "@app.get('/health')\n"            # 3
    "@cached\n"                        # 4
    "def health():\n"                  # 5
    "    return 'ok'\n"                # 6
    "\n"                               # 7
    "TIMEOUT = 5\n"                    # 8
    "registry = {}\n"                  # 9
    "\n"                               # 10
    "class Worker:\n"                  # 11
    "    pass\n"                       # 12
    "\n"                               # 13
    "if __name__ == '__main__':\n"     # 14
    "    health()\n"                   # 15
)


def test_python_function_span_includes_decorators():
    chunks = parse_python(PYTHON_MODULE, "app.py")
    health = next(chunk for chunk in chunks if chunk.metadata.get("symbol_name") == "health")
    assert health.start_line == 3
    assert health.text.startswith("@app.get('/health')")
    preamble = next(chunk for chunk in chunks if chunk.section_title == "module preamble")
    assert "@app.get" not in preamble.text
    assert (preamble.start_line, preamble.end_line) == (1, 1)


def test_python_module_level_code_between_and_after_defs_is_kept():
    chunks = parse_python(PYTHON_MODULE, "app.py")
    module_chunks = [chunk for chunk in chunks if chunk.chunk_type == "python_module"]
    texts = "\n".join(chunk.text for chunk in module_chunks)
    assert "TIMEOUT = 5" in texts
    assert "if __name__ == '__main__':" in texts
    covered = {line for chunk in chunks for line in range(chunk.start_line, chunk.end_line + 1)}
    non_blank = {number for number, line in enumerate(PYTHON_MODULE.split("\n"), start=1) if line.strip()}
    assert non_blank <= covered


def test_python_script_without_defs_keeps_fallback_chunk():
    chunks = parse_python("print('hi')\n", "script.py")
    assert [chunk.chunk_type for chunk in chunks] == ["python_fallback"]


# ── Structured config ─────────────────────────

import json  # noqa: E402

from incidentops.ingestion.parsers.code_parser import parse_structured_config  # noqa: E402


def _ranges(chunks):
    return {chunk.metadata.get("config_key"): (chunk.start_line, chunk.end_line) for chunk in chunks}


def test_yaml_config_chunks_have_per_key_line_ranges():
    content = "server:\n  port: 80\n  host: x\n\ndatabase:\n  url: pg\nlogging: info\n"
    ranges = _ranges(parse_structured_config(content, "config/app.yaml", source_type="config"))
    assert ranges == {"server": (1, 3), "database": (5, 6), "logging": (7, 7)}


def test_json_config_ranges_ignore_nested_keys_with_same_name():
    content = json.dumps({"a": {"b": 1}, "b": 2}, indent=2)
    lines = content.splitlines()
    ranges = _ranges(parse_structured_config(content, "config/app.json", source_type="config"))
    assert ranges["a"][0] == 2
    assert lines[ranges["b"][0] - 1].startswith('  "b"')


def test_toml_and_ini_config_ranges():
    toml_text = 'title = "x"\n\n[server]\nport = 80\n\n[database]\nurl = "pg"\n'
    assert _ranges(parse_structured_config(toml_text, "pyproject.toml", source_type="config")) == {
        "title": (1, 1),
        "server": (3, 4),
        "database": (6, 7),
    }
    ini_text = "[main]\na = 1\n\n[extra]\nb = 2\n"
    assert _ranges(parse_structured_config(ini_text, "setup.ini", source_type="config")) == {
        "main": (1, 2),
        "extra": (4, 5),
    }


def test_config_keys_beyond_limit_go_to_overflow_chunk():
    content = "\n".join(f"key{i}: {i}" for i in range(60)) + "\n"
    chunks = parse_structured_config(content, "config/big.yaml", source_type="config")
    assert len(chunks) == 51
    overflow = chunks[-1]
    assert overflow.metadata["kind"] == "config_overflow"
    assert overflow.metadata["config_keys"] == [f"key{i}" for i in range(50, 60)]
    assert "key59" in overflow.text
    assert (overflow.start_line, overflow.end_line) == (51, 60)


# ── Log, deploy and incident parsers ──────────

from datetime import datetime, timezone  # noqa: E402

from incidentops.ingestion.parsers.deploy_parser import parse_deploy_history  # noqa: E402
from incidentops.ingestion.parsers.incident_parser import parse_incident  # noqa: E402
from incidentops.ingestion.parsers.log_parser import parse_logs  # noqa: E402


def test_log_timestamps_are_timezone_aware_utc():
    content = (
        "2026-03-14T10:00:00Z ERROR checkout.api boom\n"
        "2026-03-14T10:00:05 INFO checkout.api naive\n"
        "2026-03-14T12:00:10+02:00 INFO checkout.api offset\n"
    )
    chunk = parse_logs(content, "logs/checkout.log")[0]
    assert chunk.timestamp_start == datetime(2026, 3, 14, 10, 0, 0, tzinfo=timezone.utc)
    assert chunk.timestamp_end == datetime(2026, 3, 14, 10, 0, 10, tzinfo=timezone.utc)
    assert chunk.timestamp_start.tzinfo is not None


@pytest.mark.parametrize("value", [1773482400, 1773482400.0, 1773482400000])
def test_deploy_history_accepts_epoch_deployed_at(value):
    chunks = parse_deploy_history(json.dumps([{"deploy_hash": "abc123", "deployed_at": value}]), "deploys/history.json")
    assert chunks[0].timestamp_start == datetime(2026, 3, 14, 10, 0, tzinfo=timezone.utc)


def test_deploy_history_naive_iso_is_utc():
    chunks = parse_deploy_history(json.dumps([{"deploy_hash": "abc123", "deployed_at": "2026-03-14T10:00:00"}]), "d.json")
    assert chunks[0].timestamp_start == datetime(2026, 3, 14, 10, 0, tzinfo=timezone.utc)


def test_incident_service_is_not_taken_from_h1_word():
    chunks = parse_incident("# Summary\nservice was slow\n## Root Cause\nDisk\n", "incidents/x.md")
    assert all(chunk.service_name is None for chunk in chunks)


def test_incident_service_from_service_line_and_front_matter():
    chunks = parse_incident("# Outage\n## Summary\nService: Checkout-API\n", "incidents/x.md")
    assert {chunk.service_name for chunk in chunks} == {"checkout-api"}
    front = "---\ntitle: Outage\nservice: payments\n---\n# Outage\n## Summary\nslow\n"
    chunks = parse_incident(front, "incidents/y.md")
    assert {chunk.service_name for chunk in chunks} == {"payments"}
