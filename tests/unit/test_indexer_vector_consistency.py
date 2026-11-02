from __future__ import annotations

import asyncio
import hashlib
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.sql import Select, Update

from incidentops.config.settings import Settings
from incidentops.db.models import Document, IndexJob
from incidentops.ingestion import indexer
from incidentops.ingestion.normalized import NormalizedDocument
from incidentops.retrieval.vector_store import VectorStoreError

PROJECT_ID = uuid.UUID("a64f9731-a4f8-4f7d-a6df-4d66fdf39e70")
SOURCE_ID = uuid.UUID("e33d8a66-f48a-4c3b-bca5-21c84cd1861d")
SYNC_ID = uuid.UUID("329af06b-4978-462b-a606-91854b3c2b0e")


class _Result:
    def __init__(self, value=None, rows=None):
        self._value = value
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))

    def all(self):
        return list(self._rows)


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeSession:
    def __init__(self, log: list, existing: Document | None = None, select_rows=None):
        self.log = log
        self.existing = existing
        self.select_rows = select_rows or []
        self.added: list = []
        self.executed: list = []

    async def execute(self, statement):
        self.executed.append(statement)
        if isinstance(statement, Select):
            if self.select_rows:
                return _Result(rows=self.select_rows.pop(0))
            return _Result(self.existing)
        return _Result()

    def add(self, obj):
        self.added.append(obj)

    def add_all(self, objs):
        self.added.extend(objs)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    def begin_nested(self):
        return _Nested()

    async def commit(self):
        self.log.append(("commit",))


class FakeVectorStore:
    fail_upsert = False
    log: list = []

    def __init__(self, _settings=None):
        pass

    async def upsert(self, points, embedding_dim):
        self.log.append(("upsert", [point["id"] for point in points]))
        if self.fail_upsert:
            raise VectorStoreError("qdrant down")

    async def delete_by_filter(self, project_id, **filters):
        self.log.append(("delete_by_filter", filters))

    async def delete_document_points_except(self, project_id, document_id, keep_point_ids):
        self.log.append(("delete_except", str(document_id), list(keep_point_ids)))


SETTINGS = Settings(qdrant_url="https://qdrant.example.test", embedding_dim=4, vector_index_version="v-test")


@pytest.fixture
def log(monkeypatch):
    events: list = []
    FakeVectorStore.log = events
    FakeVectorStore.fail_upsert = False
    monkeypatch.setattr(indexer, "QdrantVectorStore", FakeVectorStore)
    monkeypatch.setattr(indexer, "get_settings", lambda: SETTINGS)

    async def _noop(_project_id):
        return None

    monkeypatch.setattr(indexer, "invalidate_project", _noop)
    return events


def _doc(content: str = "# Runbook\n\nRestart the history service when latency spikes.\n") -> NormalizedDocument:
    return NormalizedDocument(
        external_id="runbooks/history.md",
        path="runbooks/history.md",
        source_type="runbook",
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )


async def _embed(texts):
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def _existing(content_hash: str = "0" * 64) -> Document:
    return Document(
        id=uuid.uuid4(),
        project_id=PROJECT_ID,
        source_id=SOURCE_ID,
        external_id="runbooks/history.md",
        path="runbooks/history.md",
        doc_type="runbook",
        content_hash=content_hash,
        checksum=content_hash,
    )


def _kinds(events):
    return [event[0] for event in events]


def _hash_restores(db) -> list:
    return [
        statement
        for statement in db.executed
        if isinstance(statement, Update) and "content_hash" in str(statement)
    ]


@pytest.mark.asyncio
async def test_new_document_commits_postgres_before_publishing_vectors(log):
    db = FakeSession(log)

    result = await indexer.index_normalized_documents(db, PROJECT_ID, SOURCE_ID, SYNC_ID, [_doc()], embed_fn=_embed)

    assert result.created == 1
    assert _kinds(log)[:3] == ["commit", "upsert", "delete_except"]
    upserted_ids = log[1][1]
    assert log[2][2] == upserted_ids


@pytest.mark.asyncio
async def test_changed_document_deletes_only_stale_points_after_upsert(log):
    current = _existing()
    db = FakeSession(log, existing=current)

    result = await indexer.index_normalized_documents(db, PROJECT_ID, SOURCE_ID, SYNC_ID, [_doc()], embed_fn=_embed)

    assert result.updated == 1
    assert "delete_by_filter" not in _kinds(log)
    assert _kinds(log)[:3] == ["commit", "upsert", "delete_except"]
    assert log[2][1] == str(current.id)


@pytest.mark.asyncio
async def test_qdrant_failure_after_commit_queues_retry_index_job(log):
    FakeVectorStore.fail_upsert = True
    db = FakeSession(log)
    document = _doc()

    result = await indexer.index_normalized_documents(
        db, PROJECT_ID, SOURCE_ID, SYNC_ID, [document], embed_fn=_embed
    )

    assert result.created == 1
    assert not result.errors
    assert result.diagnostics["vector_publish_deferred"] == 1
    jobs = [obj for obj in db.added if isinstance(obj, IndexJob)]
    assert len(jobs) == 1
    job = jobs[0]
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.error_code == "vector_index_failed"
    assert job.content_hash == document.content_hash
    assert job.normalized_payload_json["external_id"] == document.external_id
    assert job.normalized_payload_json[indexer.INDEX_REASON_KEY] == indexer.INDEX_REASON_VECTOR_RETRY
    # The document's hash stays clear so the retry (or the next sync) is not skipped as unchanged.
    document_row = next(obj for obj in db.added if isinstance(obj, Document))
    assert document_row.content_hash is None
    assert not _hash_restores(db)
    assert _kinds(log)[-1] == "commit"


@pytest.mark.asyncio
async def test_retry_job_is_failed_once_attempts_are_exhausted(log):
    FakeVectorStore.fail_upsert = True
    db = FakeSession(log)

    await indexer.index_normalized_documents(
        db,
        PROJECT_ID,
        SOURCE_ID,
        SYNC_ID,
        [_doc()],
        embed_fn=_embed,
        vector_retry_attempts=SETTINGS.rag_index_max_retries,
    )

    job = next(obj for obj in db.added if isinstance(obj, IndexJob))
    assert job.status == "failed"
    assert job.finished_at is not None


@pytest.mark.asyncio
async def test_unchanged_document_is_marked_seen_by_sync(log):
    document = _doc()
    current = _existing(document.content_hash)
    db = FakeSession(log, existing=current)

    result = await indexer.index_normalized_documents(db, PROJECT_ID, SOURCE_ID, SYNC_ID, [document], embed_fn=_embed)

    assert result.skipped_unchanged == 1
    assert current.last_seen_sync_id == SYNC_ID
    assert "upsert" not in _kinds(log)


@pytest.mark.asyncio
async def test_new_document_is_marked_seen_by_sync(log):
    db = FakeSession(log)

    await indexer.index_normalized_documents(db, PROJECT_ID, SOURCE_ID, SYNC_ID, [_doc()], embed_fn=_embed)

    document_row = next(obj for obj in db.added if isinstance(obj, Document))
    assert document_row.last_seen_sync_id == SYNC_ID


@pytest.mark.asyncio
async def test_failed_document_still_counts_as_seen(log):
    current = _existing()
    db = FakeSession(log, existing=current)

    async def broken_embed(_texts):
        raise RuntimeError("provider down")

    result = await indexer.index_normalized_documents(db, PROJECT_ID, SOURCE_ID, SYNC_ID, [_doc()], embed_fn=broken_embed)

    assert result.errors
    assert current.last_seen_sync_id == SYNC_ID


@pytest.mark.asyncio
async def test_existing_seen_marker_can_be_left_alone_for_worker_jobs(log):
    previous_sync = uuid.uuid4()
    document = _doc()
    current = _existing()
    current.last_seen_sync_id = previous_sync
    db = FakeSession(log, existing=current)

    await indexer.index_normalized_documents(
        db, PROJECT_ID, SOURCE_ID, SYNC_ID, [document], embed_fn=_embed, mark_existing_seen=False
    )

    assert current.last_seen_sync_id == previous_sync


@pytest.mark.asyncio
async def test_wrong_embedding_dimension_fails_before_writing_chunks(log):
    db = FakeSession(log)

    async def wrong_dim(texts):
        return [[0.1, 0.2] for _ in texts]

    result = await indexer.index_normalized_documents(db, PROJECT_ID, SOURCE_ID, SYNC_ID, [_doc()], embed_fn=wrong_dim)

    assert result.errors and result.errors[0].code == "embedding_failed"
    assert not [obj for obj in db.added if isinstance(obj, Document)]


@pytest.mark.asyncio
async def test_prune_stale_documents_removes_rows_and_vectors(log):
    stale_ids = [uuid.uuid4(), uuid.uuid4()]
    db = FakeSession(log, select_rows=[stale_ids, [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]])

    pruned = await indexer.prune_stale_documents(db, PROJECT_ID, SOURCE_ID, SYNC_ID)

    assert pruned.document_ids == stale_ids
    assert pruned.chunks_deleted == 3
    statements = [str(statement) for statement in db.executed]
    assert any(s.startswith("DELETE FROM evidence_relations") for s in statements)
    assert any(s.startswith("DELETE FROM retrieval_results") for s in statements)
    assert any(s.startswith("DELETE FROM chunks") for s in statements)
    assert any(s.startswith("DELETE FROM documents") for s in statements)
    stale_select = statements[0]
    assert "last_seen_sync_id" in stale_select and "source_id" in stale_select

    await indexer.delete_document_vectors(PROJECT_ID, SOURCE_ID, pruned.document_ids, SETTINGS)
    _, filters = log[-1]
    assert filters["all_index_versions"] is True
    assert filters["source_id"] == str(SOURCE_ID)
    assert filters["document_id"] == [str(doc_id) for doc_id in stale_ids]


@pytest.mark.asyncio
async def test_prune_with_no_stale_documents_is_a_noop(log):
    db = FakeSession(log, select_rows=[[]])

    pruned = await indexer.prune_stale_documents(db, PROJECT_ID, SOURCE_ID, SYNC_ID)

    assert pruned.document_ids == []
    assert len(db.executed) == 1


@pytest.mark.asyncio
async def test_content_hash_is_restored_only_after_vectors_are_live(log):
    db = FakeSession(log)
    document = _doc()

    await indexer.index_normalized_documents(db, PROJECT_ID, SOURCE_ID, SYNC_ID, [document], embed_fn=_embed)

    document_row = next(obj for obj in db.added if isinstance(obj, Document))
    assert document_row.checksum == document.content_hash
    restores = _hash_restores(db)
    assert len(restores) == 1
    assert document.content_hash in restores[0].compile().params.values()
    assert _kinds(log) == ["commit", "upsert", "delete_except", "commit"]


@pytest.mark.asyncio
async def test_hash_restore_cannot_overwrite_a_concurrent_rewrite(log):
    db = FakeSession(log)

    await indexer.index_normalized_documents(db, PROJECT_ID, SOURCE_ID, SYNC_ID, [_doc()], embed_fn=_embed)

    assert "documents.checksum" in str(_hash_restores(db)[0])


@pytest.mark.asyncio
async def test_crash_between_commit_and_upsert_leaves_document_reprocessable(log, monkeypatch):
    # A worker timeout or crash here must not let the next attempt skip the
    # document as unchanged: its chunks are committed but have no vectors.
    async def cancelled(self, points, embedding_dim):
        raise asyncio.CancelledError()

    monkeypatch.setattr(FakeVectorStore, "upsert", cancelled)
    db = FakeSession(log)

    with pytest.raises(asyncio.CancelledError):
        await indexer.index_normalized_documents(db, PROJECT_ID, SOURCE_ID, SYNC_ID, [_doc()], embed_fn=_embed)

    document_row = next(obj for obj in db.added if isinstance(obj, Document))
    assert document_row.content_hash is None
    assert _kinds(log) == ["commit"]


@pytest.mark.asyncio
async def test_changed_document_hash_is_cleared_at_first_commit(log, monkeypatch):
    current = _existing()
    seen = {}

    async def spying_upsert(self, points, embedding_dim):
        seen["hash_at_upsert"] = current.content_hash

    monkeypatch.setattr(FakeVectorStore, "upsert", spying_upsert)
    db = FakeSession(log, existing=current)

    await indexer.index_normalized_documents(db, PROJECT_ID, SOURCE_ID, SYNC_ID, [_doc()], embed_fn=_embed)

    assert seen["hash_at_upsert"] is None


@pytest.mark.asyncio
async def test_worker_job_advances_seen_marker_only_forward(log):
    # sync1's late job can create a row after sync2 asked for the same document;
    # sync2's job then finds it unchanged and must still claim it, or finishing
    # sync2 would prune a document it saw.
    document = _doc()
    current = _existing(document.content_hash)
    current.last_seen_sync_id = uuid.uuid4()
    db = FakeSession(log, existing=current)

    result = await indexer.index_normalized_documents(
        db, PROJECT_ID, SOURCE_ID, SYNC_ID, [document], embed_fn=_embed, mark_existing_seen=False
    )

    assert result.skipped_unchanged == 1
    advances = [s for s in db.executed if isinstance(s, Update) and "last_seen_sync_id" in str(s)]
    assert len(advances) == 1
    compiled = str(advances[0])
    assert "started_at" in compiled and "source_syncs" in compiled
    params = advances[0].compile().params.values()
    assert SYNC_ID in params
    assert SOURCE_ID in params


@pytest.mark.asyncio
async def test_job_without_a_sync_does_not_touch_the_seen_marker(log):
    document = _doc()
    db = FakeSession(log, existing=_existing())

    await indexer.index_normalized_documents(
        db, PROJECT_ID, SOURCE_ID, None, [document], embed_fn=_embed, mark_existing_seen=False
    )

    assert not [s for s in db.executed if isinstance(s, Update) and "last_seen_sync_id" in str(s)]
