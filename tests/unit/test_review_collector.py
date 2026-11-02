"""Regression tests for Collector review findings."""

from __future__ import annotations

import importlib
import types
from pathlib import Path

import httpx
import pytest

from incidentops.collector import service as collector_service
from incidentops.collector.metadata import extract
from incidentops.collector.models import CollectorSummary, DiscoveredFile
from incidentops.collector.redaction import redact
from incidentops.collector.service import CollectorService
from incidentops.config.settings import Settings
from incidentops.ingestion.failure_taxonomy import normalize_failure_code
from incidentops.security.secret_redaction import redact_secrets


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, base_url: str, token: str):
        self.uploads: list[list[dict]] = []
        self.finished: list[tuple[str, dict]] = []
        self.upload_effects: list = []
        self.fail_start = False
        FakeClient.instances.append(self)

    def capabilities(self):
        return {"limits": {}}

    def register_collector(self, *args):
        return "collector-1"

    def register_source(self, *args):
        return "source-1"

    def start_sync(self, *args):
        if self.fail_start:
            raise RuntimeError("start failed")
        return "sync-1"

    def upload(self, source_id, sync_id, collector_id, documents):
        self.uploads.append(documents)
        effect = self.upload_effects.pop(0) if self.upload_effects else None
        if isinstance(effect, Exception):
            raise effect
        if callable(effect):
            return effect(documents)
        return effect or {"created": len(documents), "updated": 0, "skipped_unchanged": 0, "chunks_created": 1}

    def finish_sync(self, source_id, sync_id, collector_id, status, diagnostics):
        self.finished.append((status, diagnostics))

    def close(self):
        pass


@pytest.fixture
def fake_client(monkeypatch):
    FakeClient.instances = []
    configure: dict = {}

    class Configured(FakeClient):
        def __init__(self, base_url, token):
            super().__init__(base_url, token)
            self.upload_effects = list(configure.get("upload_effects", []))
            self.fail_start = configure.get("fail_start", False)

    monkeypatch.setenv("INCIDENTOPS_API_URL", "http://core.test")
    monkeypatch.setenv("INCIDENTOPS_TOKEN", "token")
    monkeypatch.setattr(collector_service, "CoreCollectorClient", Configured)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    return configure


def _sync(root: Path, **kwargs) -> CollectorSummary:
    return CollectorService(Settings()).sync(
        root,
        project_id="p",
        source_name="s",
        collector_name="c",
        environment="test",
        repo_name="repo",
        branch="main",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        **kwargs,
    )


def _write_docs(root: Path, count: int) -> None:
    (root / "a.png").write_bytes(b"\x89PNG\x00")
    (root / "b.bin").write_bytes(b"data")
    for index in range(count):
        (root / f"doc{index}.md").write_text(f"# Doc {index}\nbody\n", encoding="utf-8")


# ── 9: commit SHA only from explicit/git value ─


def test_metadata_does_not_take_commit_sha_from_file_content():
    metadata = extract("docs/notes.md", "build id deadbeefcafe1234 and color ffffff00")
    assert "commit_sha" not in metadata


def test_metadata_keeps_explicit_commit_sha():
    assert extract("docs/notes.md", "deadbeefcafe", commit_sha="abc1234")["commit_sha"] == "abc1234"


# ── 10: failed syncs are finished ─────────────


def test_sync_failure_finishes_sync_as_failed_and_reraises(tmp_path, fake_client):
    _write_docs(tmp_path, 1)
    fake_client["upload_effects"] = [ValueError("boom with secret content")]
    with pytest.raises(ValueError):
        _sync(tmp_path)
    client = FakeClient.instances[-1]
    assert [status for status, _ in client.finished] == ["failed"]
    assert client.finished[0][1]["error"] == "ValueError"


def test_sync_failure_before_start_does_not_finish(tmp_path, fake_client):
    _write_docs(tmp_path, 1)
    fake_client["fail_start"] = True
    with pytest.raises(RuntimeError):
        _sync(tmp_path)
    assert FakeClient.instances[-1].finished == []


def test_daemon_loop_survives_sync_errors_with_backoff():
    from incidentops.collector.__main__ import run_daemon

    outcomes = [RuntimeError("down"), RuntimeError("down"), CollectorSummary()]
    calls: list[Path] = []

    class Service:
        def sync(self, root, **kwargs):
            calls.append(root)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    delays: list[float] = []
    args = types.SimpleNamespace(
        path=Path("/tmp/repo"),
        repo_url="",
        project_id="p",
        source_name="s",
        collector_name="c",
        environment="e",
        interval_seconds=10,
    )
    run_daemon(Service(), args, sleep=delays.append, max_iterations=3)
    assert len(calls) == 3
    assert delays[0] < delays[1]
    assert delays[2] == 10


# ── 11: counters ──────────────────────────────


def test_unchanged_documents_count_as_skipped_files(tmp_path, fake_client):
    _write_docs(tmp_path, 2)
    fake_client["upload_effects"] = [{"created": 0, "updated": 0, "skipped_unchanged": 2, "chunks_created": 0}]
    summary = _sync(tmp_path)
    assert summary.skipped_reasons["unchanged"] == 2
    assert summary.files_skipped == sum(summary.skipped_reasons.values())


def test_max_files_limits_ingested_documents_not_discovered_files(tmp_path, fake_client):
    _write_docs(tmp_path, 4)
    summary = _sync(tmp_path, max_files=2)
    assert summary.documents_normalized == 2
    inspected = CollectorService(Settings()).inspect(tmp_path, max_files=2)
    assert sum(inspected.source_type_counts.values()) == 2


def test_transient_upload_errors_are_retried_and_counted(tmp_path, fake_client):
    _write_docs(tmp_path, 1)
    request = httpx.Request("POST", "http://core.test/v1/x")
    fake_client["upload_effects"] = [
        httpx.ConnectError("refused", request=request),
        httpx.HTTPStatusError("busy", request=request, response=httpx.Response(503, request=request)),
    ]
    summary = _sync(tmp_path)
    assert summary.retry_attempted == 2
    assert summary.retry_succeeded == 1
    assert summary.documents_created == 1


def test_client_errors_are_not_retried(tmp_path, fake_client):
    _write_docs(tmp_path, 1)
    request = httpx.Request("POST", "http://core.test/v1/x")
    fake_client["upload_effects"] = [
        httpx.HTTPStatusError("bad", request=request, response=httpx.Response(400, request=request)),
    ]
    with pytest.raises(httpx.HTTPStatusError):
        _sync(tmp_path)
    assert len(FakeClient.instances[-1].uploads) == 1


def test_bytes_uploaded_excludes_rejected_documents(tmp_path, fake_client):
    _write_docs(tmp_path, 2)

    def reject_first(documents):
        return {
            "created": 1,
            "updated": 0,
            "skipped_unchanged": 0,
            "chunks_created": 1,
            "errors": [{"external_id": documents[0]["external_id"], "error": "invalid"}],
        }

    fake_client["upload_effects"] = [reject_first]
    summary = _sync(tmp_path)
    sent = FakeClient.instances[-1].uploads[0]
    assert summary.bytes_uploaded == sent[1]["size_bytes"]


# ── 12: collector skip reasons in taxonomy ────


@pytest.mark.parametrize("code", ["read_failed", "redaction_failed"])
def test_collector_skip_reasons_are_stable_failure_codes(code):
    assert normalize_failure_code(code) == code


# ── 13: one redaction pattern set ─────────────

SECRETS = {
    "ec_key": "-----BEGIN EC PRIVATE KEY-----\nMHcCAQEE\n-----END EC PRIVATE KEY-----",
    "openssh_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaA\n-----END OPENSSH PRIVATE KEY-----",
    "rsa_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
    "bearer": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
    "password": "password=hunter2",
    "api_key": "api_key=abcd1234",
}


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_collector_and_core_redact_the_same_secrets(name):
    text = f"before {SECRETS[name]} after"
    collector_text, count = redact(text)
    assert count >= 1
    assert collector_text == redact_secrets(text)
    secret_fragment = {
        "ec_key": "MHcCAQEE",
        "openssh_key": "b3BlbnNzaA",
        "rsa_key": "MIIE",
        "jwt": "dozjgNryP4J3jVmNHl0w5N",
        "bearer": "abcdefghijklmnopqrstuvwxyz012345",
        "password": "hunter2",
        "api_key": "abcd1234",
    }[name]
    assert secret_fragment not in collector_text


@pytest.mark.parametrize(
    "text",
    [
        "Checkout latency spiked to 2500ms after deploy 8f13a2",
        "def login(password: str) -> None:",
    ],
)
def test_redaction_leaves_plain_text_alone(text):
    assert redact(text) == (text, 0)


# ── 15: chunking hints are not sent ───────────


def test_normalized_documents_do_not_carry_chunking_hints(tmp_path):
    path = tmp_path / "engine.go"
    path.write_text("package history\n\nfunc Start() {}\n", encoding="utf-8")
    item = DiscoveredFile(str(path), "engine.go", path.stat().st_size, None)
    document, reason = CollectorService(Settings())._normalize(item)
    assert reason is None
    assert "chunking_hints" not in document["metadata"]
    assert "hints_are_advisory" not in document["metadata"]
    assert "hint_counts" not in CollectorSummary().diagnostics()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("incidentops.collector.hints")


# ── failure handling details ──────────────────


def test_failed_finish_does_not_mask_the_original_error(tmp_path, fake_client, monkeypatch):
    _write_docs(tmp_path, 1)
    fake_client["upload_effects"] = [ValueError("boom")]

    def broken_finish(self, *args):
        raise RuntimeError("finish down")

    monkeypatch.setattr(FakeClient, "finish_sync", broken_finish)
    with pytest.raises(ValueError):
        _sync(tmp_path)


def test_failed_sync_diagnostics_never_carry_exception_text(tmp_path, fake_client):
    _write_docs(tmp_path, 1)
    fake_client["upload_effects"] = [ValueError("boom with secret content")]
    with pytest.raises(ValueError):
        _sync(tmp_path)
    diagnostics = FakeClient.instances[-1].finished[0][1]
    assert "secret content" not in str(diagnostics)
    assert diagnostics["error_message"] == "collector sync failed: ValueError"


def test_transient_finish_errors_are_retried(tmp_path, fake_client, monkeypatch):
    _write_docs(tmp_path, 1)
    request = httpx.Request("POST", "http://core.test/v1/x")
    attempts: list[str] = []

    def flaky_finish(self, source_id, sync_id, collector_id, status, diagnostics):
        attempts.append(status)
        if len(attempts) == 1:
            raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(FakeClient, "finish_sync", flaky_finish)
    _sync(tmp_path)
    assert attempts == ["success", "success"]


def test_upload_retries_are_bounded(tmp_path, fake_client):
    _write_docs(tmp_path, 1)
    request = httpx.Request("POST", "http://core.test/v1/x")
    fake_client["upload_effects"] = [httpx.ConnectError("refused", request=request) for _ in range(20)]
    with pytest.raises(httpx.ConnectError):
        _sync(tmp_path)
    assert len(FakeClient.instances[-1].uploads) < 10
    assert FakeClient.instances[-1].finished[0][0] == "failed"


# ── a partial sync must not look complete (Core prunes unseen documents) ──


def test_full_sync_reports_complete(tmp_path, fake_client):
    _write_docs(tmp_path, 2)
    summary = _sync(tmp_path)
    assert summary.diagnostics()["complete"] is True
    assert FakeClient.instances[-1].finished[0][1]["complete"] is True


def test_max_files_truncated_sync_reports_incomplete(tmp_path, fake_client):
    _write_docs(tmp_path, 4)
    summary = _sync(tmp_path, max_files=2)
    assert summary.diagnostics()["complete"] is False
    assert FakeClient.instances[-1].finished[0][1]["complete"] is False


def test_scoped_sync_reports_incomplete(tmp_path, fake_client):
    _write_docs(tmp_path, 2)
    assert _sync(tmp_path, include_paths=["doc0.md"]).diagnostics()["complete"] is False


def test_unreadable_file_makes_sync_incomplete_and_partial(tmp_path, fake_client):
    _write_docs(tmp_path, 1)
    (tmp_path / "latin1.md").write_bytes("caf\xe9 notes\n".encode("latin-1"))
    summary = _sync(tmp_path)
    assert summary.skipped_reasons["read_failed"] == 1
    assert summary.diagnostics()["complete"] is False
    assert FakeClient.instances[-1].finished[0][0] == "partial_success"


# ── normalization edge cases ──────────────────


def test_whitespace_only_file_is_skipped_as_empty(tmp_path, fake_client):
    _write_docs(tmp_path, 1)
    (tmp_path / "blank.md").write_text("  \n\n", encoding="utf-8")
    summary = _sync(tmp_path)
    assert summary.skipped_reasons["empty"] == 1
    assert "blank.md" not in {doc["path"] for doc in FakeClient.instances[-1].uploads[0]}


def test_python_metadata_survives_source_ast_cannot_parse():
    deeply_nested = "x = " + "(" * 5000 + "1" + ")" * 5000 + "\ndef handler():\n    pass\n"
    assert "handler" in extract("deep.py", deeply_nested)["function_names"]


def test_unreadable_sample_reports_read_failed(tmp_path):
    item = DiscoveredFile(str(tmp_path / "gone.md"), "gone.md", 10, None)
    assert CollectorService(Settings())._skip_reason(item) == "read_failed"


# ── redaction: quoted keys and values, type annotations ─


@pytest.mark.parametrize(
    "text",
    [
        "def connect(password: str, retries: int) -> None:",
        "password: Optional[str] = None",
        "password: SecretStr",
        "password: string",
        "api_key: Optional[str] = None",
        "api_key: SecretStr",
    ],
)
def test_redaction_leaves_type_annotations_alone(text):
    assert redact(text) == (text, 0)


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ('{"password": "hunter2"}', "hunter2"),
        ('{"api_key": "abcd1234"}', "abcd1234"),
        ('password = "my long secret"', "long secret"),
        ("password: 'abc'", "abc"),
        ("password: strong-horse", "strong-horse"),
    ],
)
def test_redaction_covers_quoted_keys_and_values(text, secret):
    redacted, count = redact(text)
    assert count == 1
    assert secret not in redacted
