from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.sql import Select, Update

from apps.api.routes import sources
from incidentops.config.settings import Settings
from incidentops.db.models import Chunk, Document, IndexJob, ProjectRole, Source, SourceSync
from incidentops.ingestion.indexer import INDEX_REASON_KEY, INDEX_REASON_REINDEX, PrunedDocuments
from incidentops.schemas.api import (
    BatchIngestRequest,
    CollectorRegisterRequest,
    SourceCreateRequest,
    SyncFinishRequest,
    SyncStartRequest,
)

PROJECT_ID = uuid.uuid4()
SOURCE_ID = uuid.uuid4()
SYNC_ID = uuid.uuid4()
USER = SimpleNamespace(id=uuid.uuid4())


class ReachedDatabase(Exception):
    pass


class RoleChecked(Exception):
    pass


class _Result:
    def __init__(self, value=None, rows=None):
        self._value = value
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value or 0

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class FakeDB:
    def __init__(self, handler=None):
        self.handler = handler or (lambda _statement: _Result())
        self.added: list = []
        self.executed: list = []
        self.commits = 0

    async def execute(self, statement):
        self.executed.append(statement)
        return self.handler(statement)

    def add(self, obj):
        self.added.append(obj)

    def add_all(self, objs):
        self.added.extend(objs)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def refresh(self, _obj):
        return None

    async def commit(self):
        self.commits += 1


def _source() -> Source:
    return Source(id=SOURCE_ID, project_id=PROJECT_ID, name="repo", source_type="git", status="ready", sync_mode="manual")


def _sync(status: str = "running", **kwargs) -> SourceSync:
    defaults = dict(
        id=SYNC_ID,
        project_id=PROJECT_ID,
        source_id=SOURCE_ID,
        status=status,
        files_seen=0,
        files_skipped=0,
        documents_received=0,
        chunks_created=0,
        parser_errors=0,
        diagnostics_json={},
        coverage_json={},
    )
    defaults.update(kwargs)
    return SourceSync(**defaults)


@pytest.fixture
def access(monkeypatch):
    calls: dict[str, list] = {"ensure": [], "role": []}

    async def fake_ensure(db, project_id, user, settings, minimum_role=ProjectRole.viewer):
        calls["ensure"].append((project_id, user, minimum_role))

    async def fake_role(*args, **_kwargs):
        calls["role"].append(args)
        raise RoleChecked()

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sources, "ensure_project_access", fake_ensure)
    monkeypatch.setattr(sources, "require_project_role", fake_role, raising=False)
    monkeypatch.setattr(sources, "record_audit_event", noop)
    monkeypatch.setattr(sources, "record_operational_event", noop)
    monkeypatch.setattr(sources, "check_rate_limit", noop)
    monkeypatch.setattr(sources, "invalidate_project", noop)

    async def fake_source(_db, source_id):
        if source_id != SOURCE_ID:
            raise HTTPException(404, detail="Source not found")
        return _source()

    monkeypatch.setattr(sources, "_require_source", fake_source)
    return calls


def _reaches_db(_statement):
    raise ReachedDatabase()


# --- Finding 8: route access --------------------------------------------------


@pytest.mark.asyncio
async def test_start_sync_uses_ensure_project_access_with_admin(access):
    with pytest.raises(ReachedDatabase):
        await sources.start_sync(SOURCE_ID, SyncStartRequest(), None, FakeDB(_reaches_db), Settings(), USER)

    assert access["ensure"] == [(PROJECT_ID, USER, ProjectRole.admin)]
    assert access["role"] == []


@pytest.mark.asyncio
async def test_batch_uses_ensure_project_access_with_admin(access, monkeypatch):
    async def fake_sync(*_args):
        raise ReachedDatabase()

    monkeypatch.setattr(sources, "_require_sync", fake_sync)
    body = BatchIngestRequest(sync_id=SYNC_ID, documents=[])

    with pytest.raises(ReachedDatabase):
        await sources.ingest_documents_batch(SOURCE_ID, body, None, FakeDB(), Settings(), USER)

    assert access["ensure"] == [(PROJECT_ID, USER, ProjectRole.admin)]
    assert access["role"] == []


@pytest.mark.asyncio
async def test_finish_sync_uses_ensure_project_access_with_admin(access, monkeypatch):
    async def fake_sync(*_args):
        raise ReachedDatabase()

    monkeypatch.setattr(sources, "_require_sync", fake_sync)

    with pytest.raises(ReachedDatabase):
        await sources.finish_sync(SOURCE_ID, SYNC_ID, SyncFinishRequest(status="success"), None, FakeDB(), Settings(), USER)

    assert access["ensure"] == [(PROJECT_ID, USER, ProjectRole.admin)]
    assert access["role"] == []


@pytest.mark.asyncio
async def test_unknown_source_is_404(access):
    with pytest.raises(HTTPException) as excinfo:
        await sources.start_sync(uuid.uuid4(), SyncStartRequest(), None, FakeDB(), Settings(), USER)

    assert excinfo.value.status_code == 404
    assert access["ensure"] == []


@pytest.mark.asyncio
async def test_create_source_checks_admin_once(access):
    body = SourceCreateRequest(name="repo", source_type="git")

    with pytest.raises(ReachedDatabase):
        await sources.create_source(PROJECT_ID, body, None, FakeDB(_reaches_db), Settings(), USER)

    assert access["ensure"] == [(PROJECT_ID, USER, ProjectRole.admin)]
    assert access["role"] == []


@pytest.mark.asyncio
async def test_delete_source_checks_admin_once(access):
    with pytest.raises(ReachedDatabase):
        await sources.delete_source(PROJECT_ID, SOURCE_ID, None, FakeDB(_reaches_db), Settings(), USER)

    assert access["role"] == []


@pytest.mark.asyncio
async def test_reindex_source_checks_admin_once(access):
    with pytest.raises(ReachedDatabase):
        await sources.reindex_source(PROJECT_ID, SOURCE_ID, None, FakeDB(_reaches_db), Settings(), USER)

    assert access["role"] == []


@pytest.mark.asyncio
async def test_register_collector_checks_admin_once(access):
    with pytest.raises(ReachedDatabase):
        await sources.register_collector(
            PROJECT_ID, CollectorRegisterRequest(name="c1"), None, FakeDB(_reaches_db), Settings(), USER
        )

    assert access["role"] == []


# --- Finding 3: async batch path ----------------------------------------------


def _content(text: str) -> tuple[str, str]:
    return text, hashlib.sha256(text.encode()).hexdigest()


def _batch_document(external_id: str, text: str) -> dict:
    content, digest = _content(text)
    return {
        "external_id": external_id,
        "path": external_id,
        "source_type": "runbook",
        "content": content,
        "content_hash": digest,
    }


@pytest.fixture
def batch_env(access, monkeypatch):
    state = {"sync": _sync()}

    async def fake_sync(*_args):
        return state["sync"]

    async def fake_collector(*_args):
        return None

    monkeypatch.setattr(sources, "_require_sync", fake_sync)
    monkeypatch.setattr(sources, "_validate_collector", fake_collector)
    return state


@pytest.mark.asyncio
async def test_async_batch_skips_unchanged_documents_before_queueing(batch_env):
    unchanged = _batch_document("docs/a.md", "# A\n\nsame\n")
    changed = _batch_document("docs/b.md", "# B\n\nnew text\n")
    new = _batch_document("docs/c.md", "# C\n\nbrand new\n")
    existing_rows = [("docs/a.md", unchanged["content_hash"]), ("docs/b.md", "0" * 64)]

    def handler(statement):
        if isinstance(statement, Select) and "content_hash" in str(statement):
            return _Result(rows=existing_rows)
        return _Result()

    db = FakeDB(handler)
    body = BatchIngestRequest(sync_id=SYNC_ID, documents=[unchanged, changed, new])

    response = await sources.ingest_documents_batch(
        SOURCE_ID, body, None, db, Settings(rag_async_indexing=True), USER
    )

    jobs = [obj for obj in db.added if isinstance(obj, IndexJob)]
    assert sorted(job.normalized_payload_json["external_id"] for job in jobs) == ["docs/b.md", "docs/c.md"]
    assert response.skipped_unchanged == 1
    assert response.updated == 1
    assert response.created == 1
    assert response.diagnostics["index_jobs_queued"] == 2


@pytest.mark.asyncio
async def test_batch_marks_every_received_document_as_seen(batch_env):
    db = FakeDB()
    body = BatchIngestRequest(
        sync_id=SYNC_ID,
        documents=[_batch_document("docs/a.md", "# A\n\nx\n"), _batch_document("../bad", "# bad\n")],
    )

    await sources.ingest_documents_batch(SOURCE_ID, body, None, db, Settings(rag_async_indexing=True), USER)

    seen_updates = [s for s in db.executed if isinstance(s, Update) and s.table.name == "documents"]
    assert seen_updates
    params = seen_updates[0].compile().params
    assert SYNC_ID in params.values()
    assert any(value == ["docs/a.md", "../bad"] for value in params.values())


@pytest.mark.asyncio
async def test_async_batch_normalizes_duplicate_external_id_code(batch_env):
    document = _batch_document("docs/a.md", "# A\n\nx\n")
    body = BatchIngestRequest(sync_id=SYNC_ID, documents=[document, document])

    response = await sources.ingest_documents_batch(
        SOURCE_ID, body, None, FakeDB(), Settings(rag_async_indexing=True), USER
    )

    assert [error.code for error in response.errors] == ["metadata_invalid"]


@pytest.mark.asyncio
@pytest.mark.parametrize("async_indexing", [True, False])
async def test_batch_for_finished_sync_is_rejected(batch_env, async_indexing):
    batch_env["sync"] = _sync(status="success")
    body = BatchIngestRequest(sync_id=SYNC_ID, documents=[_batch_document("docs/a.md", "# A\n\nx\n")])

    with pytest.raises(HTTPException) as excinfo:
        await sources.ingest_documents_batch(
            SOURCE_ID, body, None, FakeDB(), Settings(rag_async_indexing=async_indexing), USER
        )

    assert excinfo.value.status_code == 409


# --- Findings 2 and 4: finish_sync --------------------------------------------


@pytest.fixture
def finish_env(access, monkeypatch):
    state = {
        "sync": _sync(documents_received=3),
        "pruned": [],
        "vector_deletes": [],
    }

    async def fake_sync(*_args):
        return state["sync"]

    async def fake_prune(db, project_id, source_id, sync_id):
        state["pruned"].append((project_id, source_id, sync_id, db.commits))
        return PrunedDocuments(document_ids=[uuid.uuid4()], chunks_deleted=4)

    async def fake_vectors(project_id, source_id, document_ids, settings):
        state["vector_deletes"].append((project_id, source_id, list(document_ids)))
        return True

    monkeypatch.setattr(sources, "_require_sync", fake_sync)
    monkeypatch.setattr(sources, "prune_stale_documents", fake_prune)
    monkeypatch.setattr(sources, "delete_document_vectors", fake_vectors)
    return state


@pytest.mark.asyncio
async def test_finish_sync_merges_collector_diagnostics_instead_of_overwriting(finish_env):
    finish_env["sync"].diagnostics_json = {
        "documents_created": 5,
        "source_type_counts": {"code": 3},
        "chunk_type_counts": {"go_function": 9},
        "index_jobs_completed": 2,
        "embedding_backend": "core-model",
    }
    body = SyncFinishRequest(
        status="success",
        diagnostics={
            "documents_created": 4,
            "source_type_counts": {"code": 2, "logs": 1},
            "redaction_count": 7,
            "embedding_backend": "collector-guess",
        },
    )

    await sources.finish_sync(SOURCE_ID, SYNC_ID, body, None, FakeDB(), Settings(), USER)

    diagnostics = finish_env["sync"].diagnostics_json
    assert diagnostics["documents_created"] == 5
    assert diagnostics["source_type_counts"] == {"code": 3, "logs": 1}
    assert diagnostics["chunk_type_counts"] == {"go_function": 9}
    assert diagnostics["index_jobs_completed"] == 2
    assert diagnostics["redaction_count"] == 7
    assert diagnostics["embedding_backend"] == "core-model"
    assert diagnostics["collector_reported"]["documents_created"] == 4


@pytest.mark.asyncio
async def test_successful_sync_prunes_unseen_documents_then_vectors(finish_env):
    db = FakeDB()

    await sources.finish_sync(SOURCE_ID, SYNC_ID, SyncFinishRequest(status="success"), None, db, Settings(), USER)

    assert len(finish_env["pruned"]) == 1
    project_id, source_id, sync_id, commits_before = finish_env["pruned"][0]
    assert (project_id, source_id, sync_id) == (PROJECT_ID, SOURCE_ID, SYNC_ID)
    assert commits_before == 0
    assert len(finish_env["vector_deletes"]) == 1
    assert finish_env["vector_deletes"][0][:2] == (PROJECT_ID, SOURCE_ID)
    diagnostics = finish_env["sync"].diagnostics_json
    assert diagnostics["stale_documents_deleted"] == 1
    assert diagnostics["stale_chunks_deleted"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_failed_or_cancelled_sync_does_not_prune(finish_env, status):
    await sources.finish_sync(SOURCE_ID, SYNC_ID, SyncFinishRequest(status=status), None, FakeDB(), Settings(), USER)

    assert finish_env["pruned"] == []


@pytest.mark.asyncio
async def test_sync_without_received_documents_does_not_prune(finish_env):
    finish_env["sync"].documents_received = 0

    await sources.finish_sync(SOURCE_ID, SYNC_ID, SyncFinishRequest(status="success"), None, FakeDB(), Settings(), USER)

    assert finish_env["pruned"] == []
    assert finish_env["sync"].diagnostics_json["stale_prune_skipped"] == "no_documents_received"


# --- Finding 6: reindex re-embeds -------------------------------------------------


def _chunk(document_id: uuid.UUID, text: str) -> Chunk:
    return Chunk(
        id=uuid.uuid4(),
        project_id=PROJECT_ID,
        source_id=SOURCE_ID,
        document_id=document_id,
        chunk_type="markdown_section",
        text=text,
        metadata_json={"source_type": "runbook"},
    )


@pytest.mark.asyncio
async def test_reindex_sync_mode_reembeds_chunks_and_reports_count(access, monkeypatch):
    document_id = uuid.uuid4()
    chunks = [_chunk(document_id, "alpha"), _chunk(document_id, "beta"), _chunk(document_id, "gamma")]
    pages = [[(chunk, "docs/a.md") for chunk in chunks], []]
    upserts: list = []

    def handler(statement):
        text = str(statement)
        if isinstance(statement, Select) and "JOIN documents" in text:
            return _Result(rows=pages.pop(0) if pages else [])
        if isinstance(statement, Select) and "count" in text.lower():
            return _Result(value=3)
        return _Result()

    async def fake_embed(texts, model_name=None, **_kwargs):
        return [[0.5] * 4 for _ in texts]

    class FakeStore:
        def __init__(self, _settings):
            pass

        async def upsert(self, points, embedding_dim):
            upserts.append((points, embedding_dim))

    monkeypatch.setattr(sources, "embed_texts_async", fake_embed)
    monkeypatch.setattr(sources, "QdrantVectorStore", FakeStore)

    async def no_latest(*_args):
        return None

    monkeypatch.setattr(sources, "_latest_sync_for_source", no_latest)

    async def no_counts(*_args):
        return {}

    monkeypatch.setattr(sources, "_source_type_counts", no_counts)
    monkeypatch.setattr(sources, "_chunk_type_counts", no_counts)

    response = await sources.reindex_source(
        PROJECT_ID, SOURCE_ID, None, FakeDB(handler), Settings(embedding_dim=4, rag_async_indexing=False), USER
    )

    assert response.chunks_created == 3
    assert response.diagnostics["reindex_mode"] == "reembed_inline"
    assert sum(len(points) for points, _ in upserts) == 3
    assert {point["id"] for points, _ in upserts for point in points} == {str(chunk.id) for chunk in chunks}


@pytest.mark.asyncio
async def test_reindex_async_mode_queues_index_jobs_from_stored_payloads(access, monkeypatch):
    content, digest = _content("# A\n\nx\n")
    document = Document(id=uuid.uuid4(), project_id=PROJECT_ID, source_id=SOURCE_ID, external_id="docs/a.md", content_hash=digest)
    stored = IndexJob(
        project_id=PROJECT_ID,
        source_id=SOURCE_ID,
        normalized_payload_json={
            "external_id": "docs/a.md",
            "path": "docs/a.md",
            "source_type": "runbook",
            "content": content,
            "content_hash": digest,
        },
        content_hash=digest,
        index_version="current",
    )
    document_pages = [[document], []]

    def handler(statement):
        text = str(statement)
        if isinstance(statement, Select) and "FROM index_jobs" in text:
            return _Result(rows=[stored])
        if isinstance(statement, Select) and text.startswith("SELECT documents.id") and "count" not in text.lower():
            return _Result(rows=document_pages.pop(0) if document_pages else [])
        if isinstance(statement, Select) and "count" in text.lower():
            return _Result(value=1)
        return _Result()

    async def no_latest(*_args):
        return None

    async def no_counts(*_args):
        return {}

    monkeypatch.setattr(sources, "_latest_sync_for_source", no_latest)
    monkeypatch.setattr(sources, "_source_type_counts", no_counts)
    monkeypatch.setattr(sources, "_chunk_type_counts", no_counts)
    db = FakeDB(handler)

    response = await sources.reindex_source(
        PROJECT_ID, SOURCE_ID, None, db, Settings(rag_async_indexing=True), USER
    )

    jobs = [obj for obj in db.added if isinstance(obj, IndexJob)]
    assert len(jobs) == 1
    assert jobs[0].normalized_payload_json[INDEX_REASON_KEY] == INDEX_REASON_REINDEX
    assert jobs[0].status == "pending"
    assert document.content_hash is None
    assert response.diagnostics["index_jobs_queued"] == 1
    assert response.diagnostics["reindex_mode"] == "index_jobs"
