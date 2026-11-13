from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from apps.api.routes import operations as operations_route
from incidentops.config.settings import Settings
from incidentops.db.models import EvalRun, OperationalFinding, OperationalRun, OperationalRunStatus
from incidentops.operations import service


class FakeResult:
    def __init__(self, value=None) -> None:
        self._value = value

    def scalars(self):
        return list(self._value or [])

    def scalar_one(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    def __init__(self, results) -> None:
        self._results = list(results)
        self.statements: list = []
        self.added: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self._results.pop(0) if self._results else FakeResult([])

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


def _op_run(run_type: str) -> OperationalRun:
    return OperationalRun(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        run_type=run_type,
        status=OperationalRunStatus.queued,
        attempts=0,
        idempotency_key="k",
    )


def test_finding_fingerprint_includes_project_and_evidence_key():
    project_a, project_b = uuid.uuid4(), uuid.uuid4()
    args = ("retrieval_recall_regression", "high", {}, {"min_recall": 0.6}, "act")

    base = service._finding(*args, project_id=project_a, evidence_key="eval_run:1")
    assert base["fingerprint"] == service._finding(*args, project_id=project_a, evidence_key="eval_run:1")["fingerprint"]
    assert base["fingerprint"] != service._finding(*args, project_id=project_b, evidence_key="eval_run:1")["fingerprint"]
    assert base["fingerprint"] != service._finding(*args, project_id=project_a, evidence_key="eval_run:2")["fingerprint"]


@pytest.mark.asyncio
async def test_observer_reads_latest_completed_eval_and_dedupes_open_findings():
    run = _op_run(service.RUN_OBSERVER)
    eval_run = EvalRun(id=uuid.uuid4(), project_id=run.project_id, summary_json={"avg_evidence_recall": 0.1})
    recall_fp = service._finding(
        "retrieval_recall_regression", "high", {}, {"min_recall": 0.6}, "x",
        project_id=run.project_id, evidence_key=f"eval_run:{eval_run.id}",
    )["fingerprint"]
    db = FakeSession(
        [
            FakeResult([]),  # source syncs
            FakeResult(2),  # failed index jobs
            FakeResult(0),  # stale index jobs
            FakeResult(eval_run),  # latest eval
            FakeResult([recall_fp]),  # existing open fingerprints
        ]
    )

    await service.run_observer(db, run, Settings(observer_min_eval_recall=0.6))

    eval_sql = str(db.statements[3].compile(compile_kwargs={"literal_binds": True}))
    assert "eval_runs.status = 'completed'" in eval_sql
    findings = [obj for obj in db.added if isinstance(obj, OperationalFinding)]
    assert [finding.finding_type for finding in findings] == ["index_job_failures"]
    assert run.summary_json["findings_count"] == 1
    assert run.summary_json["deduplicated_findings"] == 1


@pytest.mark.asyncio
async def test_logging_aggregate_reports_top_event_types_by_count():
    run = _op_run(service.RUN_LOGGING)
    events = [SimpleNamespace(category="c", severity="info", event_type=f"a_{index:02d}") for index in range(25)]
    events += [SimpleNamespace(category="c", severity="info", event_type="zz_hot") for _ in range(5)]
    db = FakeSession([FakeResult(events)])

    await service.run_logging_aggregate(db, run, Settings())

    event_types = run.summary_json["event_types"]
    assert len(event_types) == 20
    assert event_types["zz_hot"] == 5
    assert next(iter(event_types)) == "zz_hot"


@pytest.mark.asyncio
async def test_operational_enqueue_without_key_generates_unique_key(monkeypatch):
    keys: list[str | None] = []

    async def fake_access(*_args, **_kwargs):
        return None

    async def fake_create(_db, _project_id, _run_type, *, request=None, idempotency_key=None):
        keys.append(idempotency_key)
        return SimpleNamespace(status=OperationalRunStatus.completed)

    monkeypatch.setattr(operations_route, "ensure_project_access", fake_access)
    monkeypatch.setattr(operations_route, "create_operational_run", fake_create)
    monkeypatch.setattr(operations_route, "_run_response", lambda run: run)

    class Db:
        async def refresh(self, _obj):
            return None

    body = SimpleNamespace(idempotency_key=None)
    for _ in range(2):
        await operations_route._enqueue(uuid.uuid4(), service.RUN_OBSERVER, "execute_observer_agent", body, None, Db(), Settings(), None)
    await operations_route._enqueue(
        uuid.uuid4(), service.RUN_OBSERVER, "execute_observer_agent", SimpleNamespace(idempotency_key="mine"), None, Db(), Settings(), None
    )

    assert keys[0] and keys[1] and keys[0] != keys[1]
    assert keys[2] == "mine"
