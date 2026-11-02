from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.sql.dml import Delete

from apps.api.routes import evals as evals_route
from incidentops.config.settings import Settings
from incidentops.db.models import EvalRun, EvalRunCase, EvalStatus
from incidentops.eval import runner
from incidentops.retrieval.source_types import KNOWN_SOURCE_TYPES


class FakeSession:
    def __init__(self, results=None) -> None:
        self.statements: list = []
        self.added: list = []
        self._results = list(results or [])

    async def execute(self, statement):
        self.statements.append(statement)
        if self._results:
            return self._results.pop(0)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def refresh(self, _obj):
        return None


def _investigation(paths: list[str], summary: str, usage: dict | None = None):
    return SimpleNamespace(
        evidence=[{"document_path": path, "source_type": "deploy"} for path in paths],
        likely_root_cause=SimpleNamespace(summary=summary),
        suggested_fix=None,
        unknowns=[],
        hypotheses=[],
        debug={"llm_usage": usage} if usage else {},
    )


def test_legacy_case_fields_are_mapped_to_canonical_names():
    case = {
        "id": "c1",
        "question": "q",
        "expected_root_cause": "Synchronous inventory validation call",
        "must_not_claim": ["database outage"],
    }
    result = runner.evaluate_case_output(case, [], "There was a database outage")

    assert result["forbidden_hits"] == ["database outage"]
    assert result["term_coverage"] < 1.0
    assert "inventory" in result["missing_terms"]


def test_golden_cases_use_canonical_fields():
    cases = [json.loads(line) for line in runner.DEFAULT_CASES_PATH.read_text().splitlines() if line.strip()]
    assert cases
    for case in cases:
        assert "expected_root_cause" not in case and "must_not_claim" not in case
        assert case["expected_terms"] and case["forbidden_terms"]
        assert case["expected_source_types"]
        assert set(case["expected_source_types"]) <= KNOWN_SOURCE_TYPES


@pytest.mark.asyncio
async def test_eval_run_is_idempotent_and_uses_pass_thresholds(monkeypatch):
    responses = {
        "good": _investigation(["deploys/a.patch"], "synchronous inventory", {"prompt_tokens": 100, "completion_tokens": 20}),
        "bad_recall": _investigation([], "synchronous inventory"),
        "forbidden": _investigation(["deploys/a.patch"], "synchronous inventory database outage"),
    }

    async def fake_investigate(_db, _project_id, question, **_kwargs):
        return responses[question], 5

    monkeypatch.setattr(runner, "investigate", fake_investigate)
    cases = [
        {"id": name, "question": name, "expected_documents": ["deploys/a.patch"], "expected_terms": ["inventory"], "forbidden_terms": ["database outage"]}
        for name in responses
    ]
    db = FakeSession()
    run = EvalRun(id=uuid.uuid4(), project_id=uuid.uuid4(), status=EvalStatus.queued, summary_json={})

    await runner.execute_eval_run(db, run, cases=cases, settings=Settings())

    deletes = [stmt for stmt in db.statements if isinstance(stmt, Delete)]
    assert len(deletes) == 1 and deletes[0].table.name == "eval_run_cases"
    assert len([obj for obj in db.added if isinstance(obj, EvalRunCase)]) == 3
    summary = run.summary_json
    assert summary["passed_cases"] == 1
    assert summary["failed_cases"] == 2
    assert summary["input_tokens"] == 100
    assert summary["output_tokens"] == 20
    assert summary["model_call_count"] == 1


@pytest.mark.asyncio
async def test_case_error_in_summary_does_not_leak_exception_text(monkeypatch):
    async def failing_investigate(*_args, **_kwargs):
        raise RuntimeError("postgres://user:hunter2@db failed")

    monkeypatch.setattr(runner, "investigate", failing_investigate)
    run = EvalRun(id=uuid.uuid4(), project_id=uuid.uuid4(), status=EvalStatus.queued, summary_json={})

    await runner.execute_eval_run(FakeSession(), run, cases=[{"id": "c1", "question": "q"}], settings=Settings())

    assert "hunter2" not in json.dumps(run.summary_json, default=str)
    assert run.summary_json["results"][0]["error"] == "runtimeerror"
    assert run.summary_json["passed_cases"] == 0


def test_inline_cases_validation_enforces_size_and_shape():
    settings = Settings(max_eval_cases_bytes=200)
    assert evals_route._validate_inline_cases([{"question": "why?"}], settings) == [{"question": "why?"}]
    with pytest.raises(HTTPException) as too_big:
        evals_route._validate_inline_cases([{"question": "x" * 500}], settings)
    assert too_big.value.status_code == 400
    with pytest.raises(HTTPException):
        evals_route._validate_inline_cases([{"id": "no-question"}], settings)


@pytest.mark.asyncio
async def test_list_evals_filters_by_project_id():
    project_id = uuid.uuid4()
    db = FakeSession()
    user = SimpleNamespace(id=uuid.uuid4())

    assert await evals_route.list_evals(project_id=project_id, db=db, user=user) == []

    compiled = db.statements[0].compile()
    assert project_id in compiled.params.values()
