from __future__ import annotations

import asyncio

import pytest

from incidentops.agent.service import execute_node_with_observability
from incidentops.agent.state import InvestigationState
from incidentops.config.settings import Settings
from incidentops.observability.metrics import incr, prometheus_text, summary
from incidentops.observability.tracing import traced
from incidentops.worker.jobs import execute_job
from incidentops.worker.queue import InlineQueue


@pytest.mark.asyncio
async def test_inline_queue_round_trip_and_failure_tracking():
    queue = InlineQueue()
    job_id = await queue.enqueue("execute_workflow_run", {"run_id": "r1"})
    job = await queue.dequeue()
    assert job is not None
    assert job.id == job_id
    assert job.job_type == "execute_workflow_run"
    assert job.payload["run_id"] == "r1"
    assert await queue.dequeue() is None

    await queue.fail(job_id, "boom")
    assert queue.failed[job_id] == "boom"


@pytest.mark.asyncio
async def test_invalid_job_type_fails_safely():
    with pytest.raises(ValueError, match="Unsupported job type"):
        await execute_job(await _job("unknown", {}), Settings())


@pytest.mark.asyncio
async def test_node_wrapper_retries_safe_node(monkeypatch):
    events: list[tuple[str, str | None, dict]] = []

    async def fake_append_run_event(_db, _run_id, event_type, node_name=None, payload=None):
        events.append((event_type, node_name, payload or {}))

    monkeypatch.setattr("incidentops.agent.service.append_run_event", fake_append_run_event)
    calls = {"count": 0}

    def flaky_node(state):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary failure")
        state.status = "completed"
        return state

    state = InvestigationState(run_id="r1", project_id="p1", user_query="what happened?")
    result = await execute_node_with_observability(
        None,
        "r1",
        "classify_task",
        flaky_node,
        state,
        Settings(workflow_max_retries=1, workflow_node_timeout_seconds=5),
    )

    assert result.status == "completed"
    assert calls["count"] == 2
    assert [event[0] for event in events] == [
        "node_started",
        "node_failed",
        "node_retried",
        "node_started",
        "node_completed",
    ]


@pytest.mark.asyncio
async def test_node_wrapper_timeout_records_failure(monkeypatch):
    events: list[str] = []

    async def fake_append_run_event(_db, _run_id, event_type, node_name=None, payload=None):
        events.append(event_type)

    monkeypatch.setattr("incidentops.agent.service.append_run_event", fake_append_run_event)

    async def slow_node(state):
        await asyncio.sleep(0.05)
        return state

    with pytest.raises(RuntimeError, match="slow_node"):
        await execute_node_with_observability(
            None,
            "r1",
            "slow_node",
            slow_node,
            InvestigationState(run_id="r1", project_id="p1", user_query="what happened?"),
            Settings(workflow_max_retries=0, workflow_node_timeout_seconds=0),
        )

    assert "node_failed" in events
    assert "node_completed" not in events


def test_metrics_summary_and_prometheus_text_include_counters():
    incr("runtime_test_counter")
    data = summary()
    assert data["counters"]["runtime_test_counter"] >= 1
    text = prometheus_text()
    assert "incidentops_runtime_test_counter_total" in text


def test_tracing_disabled_context_manager_is_noop():
    with traced("unit.noop"):
        assert True


async def _job(job_type: str, payload: dict):
    queue = InlineQueue()
    await queue.enqueue(job_type, payload)
    job = await queue.dequeue()
    assert job is not None
    return job
