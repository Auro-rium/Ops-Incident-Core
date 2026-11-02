from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select, Update

from incidentops.config.settings import Settings
from incidentops.db.models import Document, IndexJob, Source, SourceSync
from incidentops.ingestion.indexer import INDEX_REASON_KEY, INDEX_REASON_VECTOR_RETRY
from incidentops.ingestion.normalized import BatchIngestResult, DocumentBatchError
from incidentops.worker import index_dispatch, jobs

PROJECT_ID = uuid.uuid4()
SOURCE_ID = uuid.uuid4()
SYNC_ID = uuid.uuid4()
CONTENT = "# Runbook\n\nRestart history.\n"
CONTENT_HASH = hashlib.sha256(CONTENT.encode()).hexdigest()


def _payload(**extra) -> dict:
    return {
        "external_id": "runbooks/history.md",
        "path": "runbooks/history.md",
        "source_type": "runbook",
        "content": CONTENT,
        "content_hash": CONTENT_HASH,
        **extra,
    }


def _job(attempts: int = 0, payload: dict | None = None) -> IndexJob:
    return IndexJob(
        id=uuid.uuid4(),
        project_id=PROJECT_ID,
        source_id=SOURCE_ID,
        sync_id=SYNC_ID,
        normalized_payload_json=payload or _payload(),
        content_hash=CONTENT_HASH,
        index_version="v-test",
        status="queued",
        attempts=attempts,
    )


class _Result:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def first(self):
        return None

    def scalars(self):
        return iter([self._value] if self._value is not None else [])


class FakeSession:
    def __init__(self, world: dict, log: list):
        self.world = world
        self.log = log
        self.executed: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, statement):
        self.executed.append(statement)
        self.log.append(("execute", statement))
        if isinstance(statement, Select):
            entity = statement.column_descriptions[0]["entity"]
            if entity is IndexJob and statement.column_descriptions[0]["name"] == "IndexJob":
                return _Result(self.world.get("job"))
            if entity is SourceSync:
                return _Result(self.world.get("sync"))
            if entity is Document:
                return _Result(self.world.get("document"))
            if entity is Source:
                return _Result(self.world.get("source"))
            return _Result()
        return _Result()

    def add(self, _obj):
        pass

    async def flush(self):
        pass

    async def commit(self):
        self.log.append(("commit", self.world["job"].status if self.world.get("job") else None))

    async def rollback(self):
        self.log.append(("rollback",))


SETTINGS = Settings(vector_index_version="v-test", rag_index_max_retries=3, rag_async_indexing=True)


@pytest.fixture
def harness(monkeypatch):
    world: dict = {}
    log: list = []
    sessions: list[FakeSession] = []

    def factory():
        session = FakeSession(world, log)
        sessions.append(session)
        return session

    monkeypatch.setattr(jobs, "_get_session_factory", lambda: factory)

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(jobs, "record_audit_event", _noop)
    monkeypatch.setattr(jobs, "record_operational_event", _noop)
    monkeypatch.setattr(jobs, "_get_embedding_batcher", lambda _settings: SimpleNamespace())
    return SimpleNamespace(world=world, log=log, sessions=sessions)


def _updates(sessions) -> list[dict]:
    values = []
    for session in sessions:
        for statement in session.executed:
            if isinstance(statement, Update) and statement.table.name == "index_jobs":
                values.append(statement.compile(dialect=postgresql.dialect()).params)
    return values


@pytest.mark.asyncio
async def test_running_state_is_committed_before_indexing(harness, monkeypatch):
    harness.world["job"] = _job()
    seen = {}

    async def fake_index(*_args, **kwargs):
        seen["commits_before"] = [entry for entry in harness.log if entry[0] == "commit"]
        seen["kwargs"] = kwargs
        return BatchIngestResult(received=1, created=1)

    monkeypatch.setattr(jobs, "index_normalized_documents", fake_index)

    await jobs.execute_index_document_job({"index_job_id": str(harness.world["job"].id)}, SETTINGS)

    assert ("commit", "running") in seen["commits_before"]
    assert seen["kwargs"]["mark_existing_seen"] is False
    assert harness.world["job"].status == "completed"


@pytest.mark.asyncio
async def test_timeout_cancellation_returns_job_to_pending(harness, monkeypatch):
    harness.world["job"] = _job(attempts=0)

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(jobs, "index_normalized_documents", hang)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            jobs.execute_index_document_job({"index_job_id": str(harness.world["job"].id)}, SETTINGS),
            timeout=0.05,
        )

    updates = _updates(harness.sessions)
    assert updates, "expected the cancelled job row to be updated"
    final = updates[-1]
    assert "pending" in final.values()
    assert "running" in final.values()  # only rows still running are touched


@pytest.mark.asyncio
async def test_timeout_cancellation_fails_job_when_attempts_exhausted(harness, monkeypatch):
    harness.world["job"] = _job(attempts=SETTINGS.rag_index_max_retries - 1)

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(jobs, "index_normalized_documents", hang)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            jobs.execute_index_document_job({"index_job_id": str(harness.world["job"].id)}, SETTINGS),
            timeout=0.05,
        )

    assert "failed" in _updates(harness.sessions)[-1].values()


@pytest.mark.asyncio
async def test_retryable_failure_sets_pending_with_incremented_attempts(harness, monkeypatch):
    harness.world["job"] = _job(attempts=0)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("embedding provider down")

    monkeypatch.setattr(jobs, "index_normalized_documents", boom)

    with pytest.raises(RuntimeError):
        await jobs.execute_index_document_job({"index_job_id": str(harness.world["job"].id)}, SETTINGS)

    final = _updates(harness.sessions)[-1]
    assert "pending" in final.values()
    assert 1 in final.values()  # attempts + 1


@pytest.mark.asyncio
async def test_worker_merges_type_counts_into_sync_diagnostics(harness, monkeypatch):
    harness.world["job"] = _job()
    sync = SourceSync(
        id=SYNC_ID,
        project_id=PROJECT_ID,
        source_id=SOURCE_ID,
        status="running",
        chunks_created=0,
        parser_errors=0,
        diagnostics_json={"source_type_counts": {"code": 2}, "chunk_type_counts": {"go_function": 5}},
    )
    harness.world["sync"] = sync

    async def fake_index(*_args, **_kwargs):
        return BatchIngestResult(
            received=1,
            created=1,
            chunks_created=2,
            source_type_counts={"runbook": 1},
            chunk_type_counts={"markdown_section": 2},
        )

    monkeypatch.setattr(jobs, "index_normalized_documents", fake_index)

    await jobs.execute_index_document_job({"index_job_id": str(harness.world["job"].id)}, SETTINGS)

    assert sync.diagnostics_json["source_type_counts"] == {"code": 2, "runbook": 1}
    assert sync.diagnostics_json["chunk_type_counts"] == {"go_function": 5, "markdown_section": 2}
    assert sync.coverage_json["has_runbooks"] is True
    assert sync.chunks_created == 2


@pytest.mark.asyncio
async def test_vector_retry_job_is_stale_when_document_was_rewritten(harness, monkeypatch):
    harness.world["job"] = _job(payload=_payload(**{INDEX_REASON_KEY: INDEX_REASON_VECTOR_RETRY}))
    harness.world["document"] = Document(id=uuid.uuid4(), content_hash="f" * 64)
    called = {"index": False}

    async def fake_index(*_args, **_kwargs):
        called["index"] = True
        return BatchIngestResult()

    monkeypatch.setattr(jobs, "index_normalized_documents", fake_index)

    await jobs.execute_index_document_job({"index_job_id": str(harness.world["job"].id)}, SETTINGS)

    assert harness.world["job"].status == "stale"
    assert called["index"] is False


@pytest.mark.asyncio
async def test_vector_retry_job_runs_while_document_hash_is_cleared(harness, monkeypatch):
    harness.world["job"] = _job(attempts=1, payload=_payload(**{INDEX_REASON_KEY: INDEX_REASON_VECTOR_RETRY}))
    harness.world["document"] = Document(id=uuid.uuid4(), content_hash=None)
    seen = {}

    async def fake_index(*_args, **kwargs):
        seen.update(kwargs)
        return BatchIngestResult(received=1, updated=1)

    monkeypatch.setattr(jobs, "index_normalized_documents", fake_index)

    await jobs.execute_index_document_job({"index_job_id": str(harness.world["job"].id)}, SETTINGS)

    assert harness.world["job"].status == "completed"
    assert seen["vector_retry_attempts"] == 2


class _DispatchSession(FakeSession):
    async def execute(self, statement):
        self.executed.append(statement)
        if isinstance(statement, Select):
            return _Result()
        return _Result()


@pytest.mark.asyncio
async def test_dispatcher_reclaims_running_jobs_past_timeout(monkeypatch):
    sessions: list[_DispatchSession] = []
    log: list = []

    def factory():
        session = _DispatchSession({}, log)
        sessions.append(session)
        return session

    monkeypatch.setattr(index_dispatch, "_get_session_factory", lambda: factory)
    settings = Settings(rag_async_indexing=True, index_job_timeout_seconds=600, rag_index_max_retries=3)
    before = datetime.now(timezone.utc)

    await index_dispatch.dispatch_recoverable_index_jobs(SimpleNamespace(), settings)

    reclaim = _updates(sessions)
    assert len(reclaim) == 2
    to_pending, to_failed = reclaim
    assert "running" in to_pending.values() and "pending" in to_pending.values()
    assert "running" in to_failed.values() and "failed" in to_failed.values()
    cutoffs = [value for value in to_pending.values() if isinstance(value, datetime)]
    assert any(abs((before - timedelta(seconds=600)) - cutoff).total_seconds() < 5 for cutoff in cutoffs)
    assert ("commit", None) in log


class _QueuedSession(_DispatchSession):
    async def execute(self, statement):
        self.executed.append(statement)
        if isinstance(statement, Select):
            return _Result(self.world.get("job"))
        return _Result()


@pytest.mark.asyncio
async def test_dispatcher_publishes_vector_retries_when_async_indexing_is_off(monkeypatch):
    # Inline indexing is the default, and that path also queues vector retries.
    job = _job(payload=_payload(**{INDEX_REASON_KEY: INDEX_REASON_VECTOR_RETRY}))
    job.status = "pending"
    world = {"job": job}
    monkeypatch.setattr(index_dispatch, "_get_session_factory", lambda: lambda: _QueuedSession(world, []))
    published: list = []

    async def enqueue(job_type, payload):
        published.append((job_type, payload))

    published_count = await index_dispatch.dispatch_recoverable_index_jobs(
        SimpleNamespace(enqueue=enqueue), Settings(rag_async_indexing=False)
    )

    assert published_count == 1
    assert published == [("index_document", {"index_job_id": str(job.id)})]


@pytest.mark.asyncio
async def test_forced_job_is_stale_when_a_newer_rewrite_also_has_a_cleared_hash(harness, monkeypatch):
    # The newer rewrite's publish failed too: content_hash alone cannot tell them apart.
    harness.world["job"] = _job(payload=_payload(**{INDEX_REASON_KEY: INDEX_REASON_VECTOR_RETRY}))
    harness.world["document"] = Document(id=uuid.uuid4(), content_hash=None, checksum="f" * 64)

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("stale job must not be indexed")

    monkeypatch.setattr(jobs, "index_normalized_documents", fail_if_called)

    await jobs.execute_index_document_job({"index_job_id": str(harness.world["job"].id)}, SETTINGS)

    assert harness.world["job"].status == "stale"


def _worker_sync(**kwargs) -> SourceSync:
    defaults = dict(
        id=SYNC_ID,
        project_id=PROJECT_ID,
        source_id=SOURCE_ID,
        status="success",
        chunks_created=4,
        parser_errors=0,
        diagnostics_json={},
    )
    defaults.update(kwargs)
    return SourceSync(**defaults)


@pytest.mark.asyncio
async def test_vector_retry_does_not_count_the_document_twice(harness, monkeypatch):
    harness.world["job"] = _job(payload=_payload(**{INDEX_REASON_KEY: INDEX_REASON_VECTOR_RETRY}))
    harness.world["document"] = Document(id=uuid.uuid4(), content_hash=None, checksum=CONTENT_HASH)
    sync = _worker_sync(diagnostics_json={"source_type_counts": {"runbook": 1}, "chunk_type_counts": {"markdown_section": 2}})
    harness.world["sync"] = sync

    async def fake_index(*_args, **_kwargs):
        return BatchIngestResult(
            received=1,
            updated=1,
            chunks_created=2,
            source_type_counts={"runbook": 1},
            chunk_type_counts={"markdown_section": 2},
        )

    monkeypatch.setattr(jobs, "index_normalized_documents", fake_index)

    await jobs.execute_index_document_job({"index_job_id": str(harness.world["job"].id)}, SETTINGS)

    assert sync.diagnostics_json["source_type_counts"] == {"runbook": 1}
    assert sync.diagnostics_json["chunk_type_counts"] == {"markdown_section": 2}
    assert sync.chunks_created == 4


@pytest.mark.asyncio
async def test_failed_document_is_not_counted_as_completed(harness, monkeypatch):
    harness.world["job"] = _job()
    sync = _worker_sync()
    harness.world["sync"] = sync

    async def fake_index(*_args, **_kwargs):
        return BatchIngestResult(
            received=1,
            skipped_invalid=1,
            errors=[DocumentBatchError(external_id="x", path="x", code="parser_exception", error="e", message="e")],
        )

    monkeypatch.setattr(jobs, "index_normalized_documents", fake_index)

    await jobs.execute_index_document_job({"index_job_id": str(harness.world["job"].id)}, SETTINGS)

    assert sync.diagnostics_json["index_jobs_failed"] == 1
    assert sync.diagnostics_json.get("index_jobs_completed", 0) == 0


@pytest.mark.asyncio
async def test_worker_merges_vector_cleanup_failures_and_locks_the_sync(harness, monkeypatch):
    harness.world["job"] = _job()
    sync = _worker_sync()
    harness.world["sync"] = sync

    async def fake_index(*_args, **_kwargs):
        return BatchIngestResult(received=1, created=1, diagnostics={"stale_vector_cleanup_failures": 2})

    monkeypatch.setattr(jobs, "index_normalized_documents", fake_index)

    await jobs.execute_index_document_job({"index_job_id": str(harness.world["job"].id)}, SETTINGS)

    assert sync.diagnostics_json["stale_vector_cleanup_failures"] == 2
    sync_selects = [
        statement
        for session in harness.sessions
        for statement in session.executed
        if isinstance(statement, Select) and statement.column_descriptions[0]["entity"] is SourceSync
    ]
    # Concurrent jobs of one sync would otherwise lose each other's increments.
    assert sync_selects and all(s._for_update_arg is not None for s in sync_selects)


def _indexing_source(status: str = "indexing") -> Source:
    return Source(id=SOURCE_ID, project_id=PROJECT_ID, name="repo", source_type="git", status=status, sync_mode="manual")


@pytest.mark.asyncio
async def test_exhausted_last_job_settles_source_out_of_indexing(harness, monkeypatch):
    harness.world["job"] = _job(attempts=SETTINGS.rag_index_max_retries - 1)
    harness.world["sync"] = _worker_sync(finished_at=datetime.now(timezone.utc))
    harness.world["source"] = _indexing_source()

    async def boom(*_args, **_kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(jobs, "index_normalized_documents", boom)

    with pytest.raises(RuntimeError):
        await jobs.execute_index_document_job({"index_job_id": str(harness.world["job"].id)}, SETTINGS)

    assert harness.world["source"].status == "error"
    assert harness.world["source"].last_sync_status == "partial_success"


@pytest.mark.asyncio
async def test_retryable_failure_leaves_source_indexing(harness, monkeypatch):
    harness.world["job"] = _job(attempts=0)
    harness.world["sync"] = _worker_sync(finished_at=datetime.now(timezone.utc))
    harness.world["source"] = _indexing_source()

    async def boom(*_args, **_kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(jobs, "index_normalized_documents", boom)

    with pytest.raises(RuntimeError):
        await jobs.execute_index_document_job({"index_job_id": str(harness.world["job"].id)}, SETTINGS)

    assert harness.world["source"].status == "indexing"


@pytest.mark.asyncio
async def test_stale_last_job_settles_source_out_of_indexing(harness):
    harness.world["job"] = _job()
    harness.world["job"].index_version = "older"
    harness.world["sync"] = _worker_sync(finished_at=datetime.now(timezone.utc))
    harness.world["source"] = _indexing_source()

    await jobs.execute_index_document_job({"index_job_id": str(harness.world["job"].id)}, SETTINGS)

    assert harness.world["job"].status == "stale"
    assert harness.world["source"].status == "ready"


@pytest.mark.asyncio
async def test_settling_does_not_overwrite_a_source_that_moved_on(harness, monkeypatch):
    # A newer sync is running, or this sync failed: the job must not claim "ready".
    harness.world["job"] = _job()
    harness.world["sync"] = _worker_sync(finished_at=datetime.now(timezone.utc))
    harness.world["source"] = _indexing_source(status="syncing")

    async def fake_index(*_args, **_kwargs):
        return BatchIngestResult(received=1, created=1)

    monkeypatch.setattr(jobs, "index_normalized_documents", fake_index)

    await jobs.execute_index_document_job({"index_job_id": str(harness.world["job"].id)}, SETTINGS)

    assert harness.world["source"].status == "syncing"


@pytest.mark.asyncio
async def test_document_writes_commit_before_the_sync_row_is_locked(harness, monkeypatch):
    # The batch route locks the sync, then documents. Holding a document update
    # while waiting for the sync lock here would deadlock against it.
    harness.world["job"] = _job()
    harness.world["sync"] = _worker_sync()

    async def fake_index(*_args, **_kwargs):
        harness.log.append(("indexed",))
        return BatchIngestResult(received=1, skipped_unchanged=1)

    monkeypatch.setattr(jobs, "index_normalized_documents", fake_index)

    await jobs.execute_index_document_job({"index_job_id": str(harness.world["job"].id)}, SETTINGS)

    after_index = harness.log[harness.log.index(("indexed",)) + 1 :]
    assert after_index[0][0] == "commit"
