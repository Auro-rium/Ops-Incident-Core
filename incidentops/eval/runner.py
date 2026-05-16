from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.config.settings import Settings, get_settings
from incidentops.db.models import EvalRun, EvalRunCase, EvalStatus
from incidentops.investigation.service import investigate
from incidentops.observability.metrics import incr, observe_latency
from incidentops.security.path_policy import validate_path_under_allowed_roots

DEFAULT_CASES_PATH = Path(__file__).parent / "golden_cases.jsonl"


def resolve_cases_path(cases_path: str | Path | None = None, settings: Settings | None = None) -> Path:
    if not cases_path:
        return DEFAULT_CASES_PATH
    if settings is None:
        return Path(cases_path)
    return validate_path_under_allowed_roots(
        str(cases_path),
        settings.eval_cases_allowed_roots,
        require_file=True,
        max_bytes=settings.max_eval_cases_bytes,
    )


def load_cases(cases_path: str | Path | None = None, settings: Settings | None = None) -> list[dict]:
    path = resolve_cases_path(cases_path, settings)
    cases = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def evaluate_case_output(case: dict, evidence_paths: list[str], answer_payload: str) -> dict:
    expected_documents = case.get("expected_documents", [])
    expected_terms = [term.lower() for term in case.get("expected_terms", [])]
    forbidden_terms = [term.lower() for term in case.get("forbidden_terms", [])]
    found_documents = [
        expected for expected in expected_documents
        if any(expected in path for path in evidence_paths)
    ]
    evidence_recall = len(found_documents) / len(expected_documents) if expected_documents else 1.0
    lower_answer = answer_payload.lower()
    found_terms = [term for term in expected_terms if term in lower_answer]
    term_coverage = len(found_terms) / len(expected_terms) if expected_terms else 1.0
    forbidden_hits = [term for term in forbidden_terms if term in lower_answer]
    return {
        "found_documents": found_documents,
        "missing_documents": [expected for expected in expected_documents if expected not in found_documents],
        "evidence_recall": evidence_recall,
        "found_terms": found_terms,
        "missing_terms": [term for term in expected_terms if term not in found_terms],
        "term_coverage": term_coverage,
        "forbidden_hits": forbidden_hits,
    }


async def run_eval_persisted(
    db: AsyncSession,
    project_id: uuid.UUID,
    top_k: int = 10,
    cases_path: str | Path | None = None,
    settings: Settings | None = None,
) -> EvalRun:
    run = await create_eval_run(db, project_id)
    return await execute_eval_run(db, run, top_k=top_k, cases_path=cases_path, settings=settings)


async def create_eval_run(db: AsyncSession, project_id: uuid.UUID) -> EvalRun:
    run = EvalRun(project_id=project_id, status=EvalStatus.queued, summary_json={})
    db.add(run)
    await db.flush()
    return run


async def execute_eval_run(
    db: AsyncSession,
    run: EvalRun,
    top_k: int = 10,
    cases_path: str | Path | None = None,
    settings: Settings | None = None,
) -> EvalRun:
    settings = settings or get_settings()
    run_started = time.time()
    incr("eval_runs_total")
    run.status = EvalStatus.running
    run.summary_json = {"status": "running"}
    await db.flush()
    try:
        cases = load_cases(cases_path, settings=settings)
    except Exception as exc:
        run.status = EvalStatus.failed
        incr("eval_failures_total")
        run.summary_json = {
            "status": "failed",
            "error": _safe_error(exc),
            "total_cases": 0,
            "passed_cases": 0,
            "failed_cases": 0,
        }
        await db.flush()
        await db.refresh(run)
        observe_latency("eval_run_duration", (time.time() - run_started) * 1000)
        return run

    results = []
    for case in cases:
        incr("eval_cases_total")
        started = time.time()
        try:
            question = case["question"]
            investigation, latency_ms = await investigate(
                db,
                run.project_id,
                question,
                top_k=top_k,
                reranker_model="",
            )
            evidence_paths = [item["document_path"] for item in investigation.evidence]
            answer_payload = " ".join(
                [
                    investigation.likely_root_cause.summary,
                    investigation.suggested_fix or "",
                    " ".join(investigation.unknowns),
                    " ".join(h.summary for h in investigation.hypotheses),
                ]
            )
            case_result = evaluate_case_output(case, evidence_paths, answer_payload)
            case_result.update(
                {
                    "case_id": case.get("id", ""),
                    "question": question,
                    "expected_documents": case.get("expected_documents", []),
                    "expected_terms": case.get("expected_terms", []),
                    "forbidden_terms": case.get("forbidden_terms", []),
                    "retrieved_documents": evidence_paths,
                    "latency_ms": latency_ms,
                    "error": None,
                }
            )
        except Exception as exc:
            case_result = {
                "case_id": str(case.get("id", "")),
                "question": str(case.get("question", "")),
                "expected_documents": case.get("expected_documents", []),
                "expected_terms": case.get("expected_terms", []),
                "forbidden_terms": case.get("forbidden_terms", []),
                "retrieved_documents": [],
                "found_documents": [],
                "missing_documents": case.get("expected_documents", []),
                "evidence_recall": 0.0,
                "found_terms": [],
                "missing_terms": [str(term).lower() for term in case.get("expected_terms", [])],
                "term_coverage": 0.0,
                "forbidden_hits": [],
                "latency_ms": int((time.time() - started) * 1000),
                "error": _safe_error(exc),
            }
        results.append(case_result)
        db.add(
            EvalRunCase(
                eval_run_id=run.id,
                case_id=str(case.get("id", f"case_{len(results)}")),
                question=str(case.get("question", "")),
                result_json=case_result,
            )
        )

    avg_recall = sum(result["evidence_recall"] for result in results) / len(results) if results else 0.0
    avg_term_coverage = sum(result["term_coverage"] for result in results) / len(results) if results else 0.0
    forbidden_hits = sum(1 for result in results if result["forbidden_hits"])
    failed_cases = sum(1 for result in results if result.get("error"))
    passed_cases = len(results) - failed_cases
    avg_latency_ms = sum(int(result.get("latency_ms") or 0) for result in results) / len(results) if results else 0.0
    run.status = EvalStatus.completed
    run.summary_json = {
        "total_cases": len(results),
        "passed_cases": passed_cases,
        "failed_cases": failed_cases,
        "avg_evidence_recall": avg_recall,
        "avg_term_coverage": avg_term_coverage,
        "forbidden_hit_count": forbidden_hits,
        "avg_latency_ms": avg_latency_ms,
        # Backward-compatible keys used by existing tests and scripts.
        "cases": len(results),
        "avg_recall": avg_recall,
        "forbidden_hit_cases": forbidden_hits,
        "results": results,
    }
    await db.flush()
    await db.refresh(run)
    observe_latency("eval_run_duration", (time.time() - run_started) * 1000)
    return run


def _safe_error(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:1000]


async def run_eval_http(
    project_id: str,
    cases_path: str | Path,
    base_url: str = "http://127.0.0.1:8000",
    top_k: int = 10,
    token: str | None = None,
) -> dict:
    cases = load_cases(cases_path)
    results = []
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(base_url=base_url, timeout=120.0, headers=headers) as client:
        for case in cases:
            response = await client.post(
                "/v1/investigate",
                json={"project_id": project_id, "query": case["question"], "top_k": top_k},
            )
            response.raise_for_status()
            payload = response.json()
            evidence_paths = [item["document_path"] for item in payload.get("evidence", [])]
            answer_payload = " ".join(
                [
                    payload.get("likely_root_cause", {}).get("summary", ""),
                    payload.get("suggested_fix", "") or "",
                    " ".join(payload.get("unknowns", [])),
                    " ".join(h.get("summary", "") for h in payload.get("hypotheses", [])),
                ]
            )
            case_result = evaluate_case_output(case, evidence_paths, answer_payload)
            case_result["case_id"] = case["id"]
            results.append(case_result)
    summary = {
        "cases": len(results),
        "avg_recall": sum(result["evidence_recall"] for result in results) / len(results) if results else 0.0,
        "avg_term_coverage": sum(result["term_coverage"] for result in results) / len(results) if results else 0.0,
        "forbidden_hit_cases": sum(1 for result in results if result["forbidden_hits"]),
        "results": results,
    }
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run IncidentOps evals against a project.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--token")
    parser.add_argument("--email")
    parser.add_argument("--password")
    return parser


async def _login_if_needed(base_url: str, token: str | None, email: str | None, password: str | None) -> str | None:
    if token:
        return token
    if not email or not password:
        return None
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        response = await client.post("/v1/auth/login", json={"email": email, "password": password})
        response.raise_for_status()
        return response.json()["access_token"]


async def _main_async() -> None:
    args = _build_parser().parse_args()
    token = await _login_if_needed(args.base_url, args.token, args.email, args.password)
    summary = await run_eval_http(
        project_id=args.project_id,
        cases_path=args.cases,
        base_url=args.base_url,
        top_k=args.top_k,
        token=token,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(_main_async())
