from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from incidentops.config.settings import Settings
from incidentops.worker import main as worker_main
from incidentops.worker.queue import FAILED_QUEUE_KEY, InlineQueue, RedisQueue
from incidentops.worker.schemas import Job


class FakePipeline:
    def __init__(self, client: "FakeRedis") -> None:
        self._client = client
        self.commands: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def xadd(self, *args, **kwargs):
        self.commands.append(("xadd", args, kwargs))
        return self

    def xack(self, *args, **kwargs):
        self.commands.append(("xack", args, kwargs))
        return self

    async def execute(self):
        self._client.transactions.append(list(self.commands))
        return [True for _ in self.commands]


class FakeRedis:
    def __init__(self) -> None:
        self.xautoclaim = AsyncMock(return_value=["0-0", []])
        self.xreadgroup = AsyncMock(return_value=[])
        self.xack = AsyncMock()
        self.xadd = AsyncMock(return_value="1-0")
        self.xgroup_create = AsyncMock()
        self.transactions: list[list[tuple]] = []
        self.pipeline_calls: list[dict] = []

    def pipeline(self, **kwargs):
        self.pipeline_calls.append(kwargs)
        return FakePipeline(self)


def _queue(client: FakeRedis) -> RedisQueue:
    queue = RedisQueue.__new__(RedisQueue)
    queue._client = client
    queue._queue_key = "jobs"
    queue._consumer_group = "group"
    queue._consumer_name = "consumer"
    queue._claim_idle_ms = 1000
    queue._max_len = 1000
    queue._group_ready = True
    queue._inflight = {}
    return queue


def _entry(job: Job, message_id: str = "5-0"):
    return (message_id, {"job": job.model_dump_json()})


@pytest.mark.asyncio
async def test_dequeue_accepts_redis_62_two_value_xautoclaim():
    client = FakeRedis()
    job = Job(job_type="execute_observer_agent", payload={"operational_run_id": "x"})
    client.xautoclaim.return_value = ["0-0", [_entry(job)]]
    queue = _queue(client)

    claimed = await queue.dequeue()

    assert claimed is not None and claimed.id == job.id
    client.xreadgroup.assert_not_awaited()


@pytest.mark.asyncio
async def test_dequeue_accepts_redis_7_three_value_xautoclaim():
    client = FakeRedis()
    job = Job(job_type="execute_observer_agent")
    client.xautoclaim.return_value = ["0-0", [_entry(job)], []]
    queue = _queue(client)

    claimed = await queue.dequeue()

    assert claimed is not None and claimed.id == job.id


@pytest.mark.asyncio
async def test_dequeue_acks_deleted_claimed_entry():
    client = FakeRedis()
    client.xautoclaim.return_value = ["0-0", [("7-0", None)]]
    queue = _queue(client)

    assert await queue.dequeue() is None
    client.xack.assert_awaited_with("jobs", "group", "7-0")


@pytest.mark.asyncio
async def test_dequeue_logs_xautoclaim_errors(caplog):
    client = FakeRedis()
    client.xautoclaim.side_effect = RuntimeError("boom")
    queue = _queue(client)

    with caplog.at_level("ERROR", logger="incidentops.worker.queue"):
        assert await queue.dequeue() is None
    assert any("reclaim" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_retry_republishes_same_job_atomically_with_backoff_and_cap():
    client = FakeRedis()
    job = Job(job_type="execute_observer_agent", payload={"operational_run_id": "x"})
    client.xreadgroup.return_value = [("jobs", [_entry(job, "9-0")])]
    queue = _queue(client)
    claimed = await queue.dequeue()
    assert claimed is not None

    await queue.retry(claimed, delay_seconds=10)

    assert client.pipeline_calls == [{"transaction": True}]
    (commands,) = client.transactions
    assert [command[0] for command in commands] == ["xadd", "xack"]
    xadd_args, xadd_kwargs = commands[0][1], commands[0][2]
    assert xadd_kwargs["maxlen"] == 1000 and xadd_kwargs["approximate"] is True
    republished = Job.model_validate_json(xadd_args[1]["job"])
    assert republished.id == job.id
    assert republished.attempts == 1
    assert republished.not_before is not None
    assert republished.not_before > datetime.now(timezone.utc) + timedelta(seconds=5)
    assert commands[1][1] == ("jobs", "group", "9-0")
    client.xadd.assert_not_awaited()


@pytest.mark.asyncio
async def test_dequeue_requeues_job_that_is_not_ready_yet():
    client = FakeRedis()
    job = Job(job_type="execute_observer_agent", not_before=datetime.now(timezone.utc) + timedelta(minutes=5))
    client.xreadgroup.return_value = [("jobs", [_entry(job, "3-0")])]
    queue = _queue(client)

    assert await queue.dequeue() is None
    (commands,) = client.transactions
    assert [command[0] for command in commands] == ["xadd", "xack"]
    assert Job.model_validate_json(commands[0][1][1]["job"]).attempts == 0
    assert queue._inflight == {}


@pytest.mark.asyncio
async def test_failed_stream_records_type_attempts_and_truncated_payload():
    client = FakeRedis()
    job = Job(job_type="execute_eval_run", attempts=2, payload={"blob": "x" * 5000})
    client.xreadgroup.return_value = [("jobs", [_entry(job, "4-0")])]
    queue = _queue(client)
    await queue.dequeue()

    await queue.fail(job.id, "boom")

    args, kwargs = client.xadd.await_args
    assert args[0] == FAILED_QUEUE_KEY
    fields = args[1]
    assert fields["job_type"] == "execute_eval_run"
    assert fields["attempts"] == "2"
    assert len(fields["payload"]) <= 2000
    assert kwargs["approximate"] is True and kwargs["maxlen"] > 0
    client.xack.assert_awaited_with("jobs", "group", "4-0")


@pytest.mark.asyncio
async def test_enqueue_caps_stream_length():
    client = FakeRedis()
    queue = _queue(client)
    await queue.enqueue("execute_observer_agent", {"a": 1})
    _, kwargs = client.xadd.await_args
    assert kwargs == {"maxlen": 1000, "approximate": True}


@pytest.mark.asyncio
async def test_inline_queue_honours_not_before():
    queue = InlineQueue()
    job = Job(job_type="x", not_before=datetime.now(timezone.utc) + timedelta(minutes=1))
    await queue.retry(job, delay_seconds=60)
    assert await queue.dequeue() is None


class RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def enqueue(self, job_type, payload):
        self.calls.append(("enqueue", job_type))
        return "new"

    async def dequeue(self):
        return None

    async def acknowledge(self, job_id):
        self.calls.append(("ack", job_id))

    async def fail(self, job_id, error):
        self.calls.append(("fail", job_id, error))

    async def retry(self, job, *, delay_seconds):
        self.calls.append(("retry", job.id, job.attempts, delay_seconds))


def _patch_execute(monkeypatch, exc: BaseException):
    async def boom(_job, _settings):
        raise exc

    monkeypatch.setattr(worker_main, "execute_job", boom)
    monkeypatch.setattr(worker_main, "_mark_operational_failure", AsyncMock())
    marked = AsyncMock()
    monkeypatch.setattr(worker_main, "_mark_workflow_failure", marked)
    return marked


@pytest.mark.asyncio
async def test_retry_uses_job_attempts_and_exponential_backoff(monkeypatch):
    _patch_execute(monkeypatch, RuntimeError("x"))
    queue = RecordingQueue()
    settings = Settings(worker_job_max_retries=3)
    job = Job(job_type="execute_observer_agent", attempts=2, payload={"operational_run_id": "r"})

    await worker_main._process_job(job, queue, settings)

    assert queue.calls == [("retry", job.id, 2, worker_main._retry_delay_seconds(2))]
    assert worker_main._retry_delay_seconds(2) > worker_main._retry_delay_seconds(1)
    assert "_worker_attempt" not in job.payload


@pytest.mark.asyncio
async def test_retry_exhausted_moves_job_to_failed(monkeypatch):
    _patch_execute(monkeypatch, RuntimeError("x"))
    queue = RecordingQueue()
    job = Job(job_type="execute_observer_agent", attempts=2, payload={"operational_run_id": "r"})

    await worker_main._process_job(job, queue, Settings(worker_job_max_retries=2))

    assert queue.calls == [("fail", job.id, "runtimeerror")]


@pytest.mark.asyncio
async def test_index_document_failures_are_acked_not_republished(monkeypatch):
    _patch_execute(monkeypatch, RuntimeError("x"))
    queue = RecordingQueue()
    job = Job(job_type="index_document", payload={"index_job_id": "i"})

    await worker_main._process_job(job, queue, Settings(worker_job_max_retries=5))

    assert queue.calls == [("ack", job.id)]


def test_workflow_outer_timeout_exceeds_run_timeout():
    settings = Settings(workflow_run_timeout_seconds=300)
    assert worker_main._job_timeout_seconds("execute_workflow_run", settings) > 300


@pytest.mark.asyncio
async def test_workflow_outer_timeout_marks_run_failed_without_retry(monkeypatch):
    marked = _patch_execute(monkeypatch, TimeoutError())
    queue = RecordingQueue()
    job = Job(job_type="execute_workflow_run", payload={"run_id": "r"})

    await worker_main._process_job(job, queue, Settings(worker_job_max_retries=3))

    marked.assert_awaited_once_with({"run_id": "r"}, "job_timeout")
    assert queue.calls == [("fail", job.id, "job_timeout")]


@pytest.mark.asyncio
async def test_workflow_retries_only_transient_errors(monkeypatch):
    _patch_execute(monkeypatch, ValueError("bad"))
    queue = RecordingQueue()
    job = Job(job_type="execute_workflow_run", payload={"run_id": "r"})
    await worker_main._process_job(job, queue, Settings(worker_job_max_retries=3))
    assert queue.calls[0][0] == "fail"

    _patch_execute(monkeypatch, ConnectionError("reset"))
    queue = RecordingQueue()
    await worker_main._process_job(job, queue, Settings(worker_job_max_retries=3))
    assert queue.calls[0][0] == "retry"

