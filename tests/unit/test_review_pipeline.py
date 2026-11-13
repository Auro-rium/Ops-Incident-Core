"""Regression tests: the local pipeline follows the Collector's rules."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from incidentops.collector.service import CollectorService
from incidentops.config.settings import Settings
from incidentops.db.models import Source, SourceSync
from incidentops.ingestion import pipeline
from incidentops.ingestion.normalized import BatchIngestResult, DocumentBatchError


def _read(base: Path, **overrides):
    return pipeline._read_local_documents(base, Settings(**overrides))


def _reasons(result) -> dict[str, str]:
    return {item["path"]: item["reason"] for item in result.skipped_files}


def _paths(result) -> set[str]:
    return {document.path for document in result.documents}


# ── discovery and skip rules ──────────────────


@pytest.mark.parametrize(
    ("relative", "reason"),
    [
        (".git/config.md", "vendor_file"),
        ("node_modules/pkg/index.js", "vendor_file"),
        (".venv/lib/site.py", "vendor_file"),
        ("dist/bundle.js", "generated_file"),
        (".env.production", "unsupported_extension"),
        ("server.key", "unsupported_extension"),
    ],
)
def test_collector_skip_rules_apply_to_local_ingest(tmp_path, relative, reason):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("TOKEN=value\n", encoding="utf-8")
    assert _reasons(_read(tmp_path))[relative] == reason


@pytest.mark.parametrize(
    "name",
    [".netrc", ".git-credentials", ".npmrc", ".pgpass", ".htpasswd", ".golangci.yml", "credentials", "id_rsa", "id_ed25519"],
)
def test_credential_files_are_never_read(tmp_path, name):
    # None of these formats match a redaction pattern, so skipping is the only protection.
    (tmp_path / name).write_text("machine host login user password hunter2\n", encoding="utf-8")
    result = _read(tmp_path)
    assert result.documents == []
    assert _reasons(result) == {name: "unsupported_extension"}
    inspected = CollectorService(Settings()).inspect(tmp_path)
    assert inspected.skipped_reasons == {"unsupported_extension": 1}


def test_files_inside_dot_directories_keep_their_own_rules(tmp_path):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "deploy.yml").write_text("on: push\n", encoding="utf-8")
    assert _paths(_read(tmp_path)) == {".github/deploy.yml"}


def test_suffixless_text_files_are_ingested_like_the_collector(tmp_path):
    (tmp_path / "Makefile").write_text("build:\n\techo hi\n", encoding="utf-8")
    result = _read(tmp_path)
    assert _paths(result) == {"Makefile"}
    assert result.documents[0].source_type == "unknown_text"
    assert result.source_type_counts == {"unknown_text": 1}


def test_suffixless_binary_files_are_skipped(tmp_path):
    (tmp_path / "program").write_bytes(b"\x7fELF\x00\x01")
    assert _reasons(_read(tmp_path)) == {"program": "binary"}


def test_deploy_paths_classify_like_the_collector(tmp_path):
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "notes.md").write_text("# Rollout\n", encoding="utf-8")
    assert _read(tmp_path).documents[0].source_type == "deploy"


# ── reading ───────────────────────────────────


def test_non_utf8_files_are_skipped_not_replaced(tmp_path):
    (tmp_path / "latin1.md").write_bytes("caf\xe9 notes\n".encode("latin-1"))
    result = _read(tmp_path)
    assert result.documents == []
    assert _reasons(result) == {"latin1.md": "read_error"}


def test_local_documents_are_redacted_before_indexing(tmp_path):
    (tmp_path / "notes.md").write_text("password=hunter2\n", encoding="utf-8")
    document = _read(tmp_path).documents[0]
    assert "hunter2" not in document.content
    assert document.metadata["redaction_count"] == 1


def test_public_skip_labels_stay_stable(tmp_path):
    (tmp_path / "huge.log").write_text("x" * 50, encoding="utf-8")
    (tmp_path / "blank.md").write_text(" \n", encoding="utf-8")
    (tmp_path / "ignore.bin").write_bytes(b"data")
    reasons = _reasons(_read(tmp_path, max_ingest_file_bytes=10))
    assert reasons == {"huge.log": "file_too_large", "blank.md": "empty_file", "ignore.bin": "unsupported_extension"}


def test_skipped_file_list_is_bounded_but_the_count_is_not(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "_MAX_LISTED_SKIPS", 3)
    modules = tmp_path / "node_modules"
    modules.mkdir()
    for index in range(10):
        (modules / f"m{index}.js").write_text("x", encoding="utf-8")
    result = _read(tmp_path)
    assert result.files_skipped == 10
    assert len(result.skipped_files) == 3


# ── sync status ───────────────────────────────


class FakeSession:
    def __init__(self):
        self.added: list = []
        self.commits = 0
        self.rollbacks = 0
        self.source: Source | None = None
        self.sync: SourceSync | None = None

    def add(self, row):
        self.added.append(row)
        if isinstance(row, SourceSync):
            self.sync = row
        if isinstance(row, Source):
            self.source = row

    async def flush(self):
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = uuid.uuid4()

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def execute(self, statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.source)

    async def get(self, model, identifier):
        return self.sync if model is SourceSync else self.source


def _error(path: str) -> DocumentBatchError:
    return DocumentBatchError(path=path, external_id=path, code="parser_exception", error="bad")


async def _run(monkeypatch, tmp_path, outcome: BatchIngestResult):
    session = FakeSession()

    async def fake_index(db, project_id, source_id, sync_id, documents, **kwargs):
        # A failed run must already have a durable sync row to mark.
        assert db.commits >= 1
        return outcome

    monkeypatch.setattr(pipeline, "index_normalized_documents", fake_index)
    stats = await pipeline.run_ingestion(session, uuid.uuid4(), str(tmp_path))
    return session, stats


async def test_clean_run_is_success(monkeypatch, tmp_path):
    (tmp_path / "a.md").write_text("# A\n", encoding="utf-8")
    session, stats = await _run(monkeypatch, tmp_path, BatchIngestResult(received=1, created=1))
    assert session.sync.status == "success"
    assert stats["status"] == "success"
    assert session.source.status == "ready"


async def test_some_document_errors_are_partial_success(monkeypatch, tmp_path):
    (tmp_path / "a.md").write_text("# A\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# B\n", encoding="utf-8")
    result = BatchIngestResult(received=2, created=1, skipped_invalid=1, errors=[_error("b.md")])
    session, _ = await _run(monkeypatch, tmp_path, result)
    assert session.sync.status == "partial_success"
    assert session.sync.parser_errors == 1


async def test_unreadable_file_is_partial_success(monkeypatch, tmp_path):
    (tmp_path / "a.md").write_text("# A\n", encoding="utf-8")
    (tmp_path / "latin1.md").write_bytes("caf\xe9\n".encode("latin-1"))
    session, _ = await _run(monkeypatch, tmp_path, BatchIngestResult(received=1, created=1))
    assert session.sync.status == "partial_success"


async def test_unchanged_documents_count_as_indexed(monkeypatch, tmp_path):
    (tmp_path / "a.md").write_text("# A\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# B\n", encoding="utf-8")
    result = BatchIngestResult(received=2, skipped_unchanged=1, skipped_invalid=1, errors=[_error("b.md")])
    session, _ = await _run(monkeypatch, tmp_path, result)
    assert session.sync.status == "partial_success"


async def test_every_document_failing_is_failed(monkeypatch, tmp_path):
    (tmp_path / "a.md").write_text("# A\n", encoding="utf-8")
    result = BatchIngestResult(received=1, skipped_invalid=1, errors=[_error("a.md")])
    session, stats = await _run(monkeypatch, tmp_path, result)
    assert session.sync.status == "failed"
    assert stats["status"] == "failed"
    assert session.source.status == "error"
    assert session.sync.error_message
    assert session.source.last_error == session.sync.error_message


async def test_folder_with_nothing_to_ingest_is_success(monkeypatch, tmp_path):
    (tmp_path / "image.png").write_bytes(b"\x89PNG\x00")
    session, _ = await _run(monkeypatch, tmp_path, BatchIngestResult())
    assert session.sync.status == "success"


async def test_index_exception_persists_failed_status_without_detail(monkeypatch, tmp_path):
    (tmp_path / "a.md").write_text("# A\n", encoding="utf-8")
    session = FakeSession()

    async def boom(*args, **kwargs):
        raise ValueError("secret detail")

    monkeypatch.setattr(pipeline, "index_normalized_documents", boom)
    with pytest.raises(ValueError):
        await pipeline.run_ingestion(session, uuid.uuid4(), str(tmp_path))
    assert session.rollbacks == 1
    assert session.sync.status == "failed"
    assert session.sync.finished_at is not None
    assert session.sync.error_message == "local ingest failed: ValueError"
    assert session.source.status == "error"
    assert session.source.last_sync_status == "failed"
    assert session.commits >= 2
