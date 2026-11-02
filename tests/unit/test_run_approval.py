from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from apps.api.routes import runs as runs_route
from incidentops.agent import service as agent_service
from incidentops.config.settings import Settings
from incidentops.db.models import (
    AgentRun,
    AgentRunEvent,
    Approval,
    ApprovalStatus,
    DraftStatus,
    IssueDraft,
    ProjectRole,
    RunStatus,
)


class FakeResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        values = self._value if isinstance(self._value, list) else [self._value] if self._value else []
        return SimpleNamespace(all=lambda: list(values), first=lambda: values[0] if values else None)


class FakeSession:
    def __init__(self, results) -> None:
        self._results = list(results)
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self._results.pop(0) if self._results else FakeResult(None)

    def add(self, _obj):
        return None

    async def flush(self):
        return None


@pytest.fixture(autouse=True)
def _no_run_events(monkeypatch):
    async def fake_append(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_service, "append_run_event", fake_append)


def _run(status: RunStatus) -> AgentRun:
    return AgentRun(id=uuid.uuid4(), project_id=uuid.uuid4(), user_query="q", status=status, pending_approval=True)


@pytest.mark.asyncio
async def test_resume_rejects_run_not_awaiting_approval():
    run = _run(RunStatus.completed)
    db = FakeSession([FakeResult(run)])
    with pytest.raises(agent_service.ApprovalConflictError):
        await agent_service.resume_after_approval(db, run, approved=True, rationale=None, resolved_by_user_id=None)


@pytest.mark.asyncio
async def test_resume_rejects_when_no_pending_approval():
    run = _run(RunStatus.awaiting_approval)
    db = FakeSession([FakeResult(run), FakeResult(None)])
    with pytest.raises(agent_service.ApprovalConflictError):
        await agent_service.resume_after_approval(db, run, approved=True, rationale=None, resolved_by_user_id=None)


@pytest.mark.parametrize(
    ("approved", "run_status", "approval_status", "draft_status"),
    [
        (True, RunStatus.completed, ApprovalStatus.approved, DraftStatus.approved),
        (False, RunStatus.rejected, ApprovalStatus.rejected, DraftStatus.rejected),
    ],
)
@pytest.mark.asyncio
async def test_resume_resolves_approval_draft_and_run(approved, run_status, approval_status, draft_status):
    run = _run(RunStatus.awaiting_approval)
    approval = Approval(run_id=run.id, action_type="create_github_issue", status=ApprovalStatus.pending)
    draft = IssueDraft(run_id=run.id, title="t", body_markdown="b", provider="github", status=DraftStatus.draft)
    resolver = uuid.uuid4()
    db = FakeSession([FakeResult(run), FakeResult(approval), FakeResult([draft])])

    result = await agent_service.resume_after_approval(db, run, approved=approved, rationale="ok", resolved_by_user_id=resolver)

    assert all(stmt._for_update_arg is not None for stmt in db.statements)
    assert approval.status == approval_status
    assert approval.resolved_by_user_id == resolver
    assert approval.resolved_at is not None
    assert draft.status == draft_status
    assert result.status == run_status
    assert result.pending_approval is False


@pytest.mark.asyncio
async def test_approve_route_maps_conflict_to_409_and_checks_access(monkeypatch):
    run = _run(RunStatus.completed)
    access_calls: list = []

    async def fake_access(_db, project_id, _user, _settings, minimum_role):
        access_calls.append((project_id, minimum_role))

    async def fake_resume(*_args, **_kwargs):
        raise agent_service.ApprovalConflictError("run is not awaiting approval")

    monkeypatch.setattr(runs_route, "ensure_project_access", fake_access)
    monkeypatch.setattr(runs_route, "resume_after_approval", fake_resume)
    db = FakeSession([FakeResult(run)])

    with pytest.raises(HTTPException) as exc_info:
        await runs_route.approve_run(
            run.id, SimpleNamespace(rationale=None), None, db=db, settings=Settings(), user=SimpleNamespace(id=uuid.uuid4())
        )

    assert exc_info.value.status_code == 409
    assert access_calls == [(run.project_id, ProjectRole.approver)]


def test_enqueue_error_does_not_leak_exception_text():
    error = runs_route._safe_error(RuntimeError("redis://user:hunter2@host failed"))
    assert "hunter2" not in error
    assert error.startswith("runtimeerror")


class _Factory:
    """Session factory whose sessions answer (status, events) per poll."""

    def __init__(self, polls) -> None:
        self._polls = list(polls)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        status, events = self._polls.pop(0) if self._polls else (RunStatus.running, [])

        class _Session:
            async def __aenter__(self):
                return FakeSession([FakeResult(status), FakeResult(events)])

            async def __aexit__(self, *_exc):
                return False

        return _Session()


def _event(seq: int) -> AgentRunEvent:
    return AgentRunEvent(
        run_id=uuid.uuid4(), sequence_no=seq, event_type=f"e{seq}", payload_json={}, created_at=datetime.now(timezone.utc)
    )


@pytest.mark.asyncio
async def test_event_stream_polls_until_run_is_terminal():
    factory = _Factory([(RunStatus.running, [_event(1), _event(2)]), (RunStatus.completed, [_event(3)])])
    chunks = [
        chunk
        async for chunk in runs_route._stream_run_events(uuid.uuid4(), factory, poll_interval=0, max_seconds=60)
    ]

    data = [json.loads(chunk.split("data: ", 1)[1]) for chunk in chunks if chunk.startswith("data: ")]
    assert [item["sequence_no"] for item in data] == [1, 2, 3]
    assert factory.calls == 2
    assert chunks[-1].startswith("event: end")


@pytest.mark.asyncio
async def test_event_stream_stops_at_time_cap():
    factory = _Factory([])
    chunks = [
        chunk
        async for chunk in runs_route._stream_run_events(uuid.uuid4(), factory, poll_interval=0, max_seconds=0)
    ]
    assert factory.calls == 1
    assert chunks[-1].startswith("event: end")
