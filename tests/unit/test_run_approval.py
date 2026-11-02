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


async def _role_granted(*_args, **_kwargs):
    return ProjectRole.approver


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
    monkeypatch.setattr(runs_route, "require_project_role", _role_granted)
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


@pytest.mark.asyncio
async def test_event_stream_ends_when_run_awaits_approval():
    factory = _Factory([(RunStatus.awaiting_approval, [_event(1)])])
    chunks = [
        chunk
        async for chunk in runs_route._stream_run_events(uuid.uuid4(), factory, poll_interval=0, max_seconds=60)
    ]
    assert factory.calls == 1
    assert chunks[-1].startswith("event: end")
    assert '"awaiting_approval"' in chunks[-1]


@pytest.mark.asyncio
async def test_resume_rereads_locked_run_instead_of_stale_identity_map_copy():
    run = _run(RunStatus.awaiting_approval)
    db = FakeSession([FakeResult(run), FakeResult(None)])
    with pytest.raises(agent_service.ApprovalConflictError):
        await agent_service.resume_after_approval(db, run, approved=True, rationale=None, resolved_by_user_id=None)
    assert db.statements[0].get_execution_options().get("populate_existing") is True


@pytest.mark.asyncio
async def test_resume_replaces_final_answer_so_jsonb_change_is_persisted():
    run = _run(RunStatus.awaiting_approval)
    original = {"question": "q"}
    run.final_answer_json = original
    approval = Approval(run_id=run.id, action_type="create_github_issue", status=ApprovalStatus.pending)
    db = FakeSession([FakeResult(run), FakeResult(approval), FakeResult([])])

    result = await agent_service.resume_after_approval(db, run, approved=True, rationale="ok", resolved_by_user_id=None)

    assert result.final_answer_json is not original
    assert result.final_answer_json["approval"] == {"status": "approved", "rationale": "ok"}
    assert approval.rationale == "ok"


@pytest.mark.asyncio
async def test_reject_route_maps_conflict_to_409_and_checks_access(monkeypatch):
    run = _run(RunStatus.rejected)
    access_calls: list = []
    resume_kwargs: list = []
    resolver = uuid.uuid4()

    async def fake_access(_db, project_id, _user, _settings, minimum_role):
        access_calls.append((project_id, minimum_role))

    async def fake_resume(*_args, **kwargs):
        resume_kwargs.append(kwargs)
        raise agent_service.ApprovalConflictError("run is not awaiting approval")

    monkeypatch.setattr(runs_route, "ensure_project_access", fake_access)
    monkeypatch.setattr(runs_route, "require_project_role", _role_granted)
    monkeypatch.setattr(runs_route, "resume_after_approval", fake_resume)
    db = FakeSession([FakeResult(run)])

    with pytest.raises(HTTPException) as exc_info:
        await runs_route.reject_run(
            run.id, SimpleNamespace(rationale="no"), None, db=db, settings=Settings(), user=SimpleNamespace(id=resolver)
        )

    assert exc_info.value.status_code == 409
    assert access_calls == [(run.project_id, ProjectRole.approver)]
    assert resume_kwargs[0]["approved"] is False
    assert resume_kwargs[0]["resolved_by_user_id"] == resolver


@pytest.mark.asyncio
async def test_stream_route_releases_request_connection_before_streaming(monkeypatch):
    run = _run(RunStatus.running)
    commits: list = []

    class CommittingSession(FakeSession):
        async def commit(self):
            commits.append(True)

    async def fake_access(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runs_route, "ensure_project_access", fake_access)
    monkeypatch.setattr(runs_route, "require_project_role", _role_granted)
    monkeypatch.setattr(runs_route, "_get_session_factory", lambda: _Factory([]))
    db = CommittingSession([FakeResult(run)])

    response = await runs_route.get_run_events(run.id, stream=True, db=db, settings=Settings(), user=None)

    assert response.media_type == "text/event-stream"
    # FastAPI keeps yield dependencies open until the stream ends.
    assert commits == [True]
    assert len(db.statements) == 1


def test_workflow_run_error_does_not_leak_exception_text():
    error = agent_service._safe_error(RuntimeError("postgres://user:hunter2@db failed"))
    assert "hunter2" not in error
    assert error == "runtimeerror"


def test_node_failure_error_names_node_without_exception_text():
    error = agent_service._safe_error(agent_service.NodeExecutionError("analyze_logs", ValueError("hunter2")))
    assert error == "node_failed:analyze_logs:valueerror"


@pytest.mark.asyncio
async def test_node_runner_raises_safe_error_after_exhausting_attempts():
    def failing_node(_state):
        raise ValueError("hunter2")

    with pytest.raises(agent_service.NodeExecutionError) as exc_info:
        await agent_service.execute_node_with_observability(
            FakeSession([]), uuid.uuid4(), "analyze_logs", failing_node, SimpleNamespace(), Settings(workflow_max_retries=0)
        )

    assert "hunter2" not in str(exc_info.value)
    assert exc_info.value.node_name == "analyze_logs"
