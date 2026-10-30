from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.config.settings import Settings, get_settings
from incidentops.db.models import EvalRun, EvalRunCase, EvalStatus
from incidentops.investigation.service import investigate
from incidentops.observability.metrics import incr, observe_latency
from incidentops.retrieval.query_intent import classify_query_intent
from incidentops.security.path_policy import validate_path_under_allowed_roots

DEFAULT_CASES_PATH = Path(__file__).parent / "golden_cases.jsonl"

# Default pass thresholds; Settings may override them with the same lower-case names.
EVAL_PASS_MIN_EVIDENCE_RECALL = 0.5
EVAL_PASS_MAX_FORBIDDEN_HITS = 0

_TERM_STOPWORDS = frozenset(
    {"about", "added", "after", "before", "call", "caused", "from", "into", "that", "this", "under", "with", "when", "were"}
)


def normalize_case(case: dict) -> dict:
    """Map legacy golden-case fields onto the canonical names the runner scores."""
    normalized = dict(case)
    if "expected_terms" not in normalized and normalized.get("expected_root_cause"):
        normalized["expected_terms"] = _root_cause_terms(str(normalized["expected_root_cause"]))
    if "forbidden_terms" not in normalized and normalized.get("must_not_claim"):
        normalized["forbidden_terms"] = [str(term) for term in normalized["must_not_claim"]]
    return normalized


def _root_cause_terms(text: str) -> list[str]:
    terms: list[str] = []
    for raw in text.lower().split():
        word = raw.strip(".,;:()[]\"'")
        if len(word) >= 4 and word not in _TERM_STOPWORDS and word not in terms:
            terms.append(word)
    return terms


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
                cases.append(normalize_case(json.loads(line)))
    return cases


def evaluate_case_output(
    case: dict,
    evidence_paths: list[str],
    answer_payload: str,
    evidence_source_types: list[str] | None = None,
) -> dict:
    case = normalize_case(case)
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
    observed_source_types = sorted(set(evidence_source_types or []))
    expected_source_types = [str(value) for value in case.get("expected_source_types", [])]
    forbidden_source_types = [str(value) for value in case.get("forbidden_source_types", [])]
    source_type_hits = sorted(set(expected_source_types) & set(observed_source_types))
    wrong_source_type_hits = sorted(set(forbidden_source_types) & set(observed_source_types))
    return {
        "found_documents": found_documents,
        "missing_documents": [expected for expected in expected_documents if expected not in found_documents],
        "evidence_recall": evidence_recall,
        "found_terms": found_terms,
        "missing_terms": [term for term in expected_terms if term not in found_terms],
        "term_coverage": term_coverage,
        "forbidden_hits": forbidden_hits,
        "query_class": str(case.get("query_class") or classify_query_intent(str(case.get("question", ""))).intent),
        "expected_source_types": expected_source_types,
        "observed_source_types": observed_source_types,
        "source_type_hit_rate": len(source_type_hits) / len(expected_source_types) if expected_source_types else 1.0,
        "wrong_source_type_hits": wrong_source_type_hits,
    }


async def run_eval_persisted(
    db: AsyncSession,
    project_id: uuid.UUID,
    top_k: int = 10,
    cases_path: str | Path | None = None,
    settings: Settings | None = None,
    cases: list[dict] | None = None,
) -> EvalRun:
    run = await create_eval_run(db, project_id)
    return await execute_eval_run(db, run, top_k=top_k, cases_path=cases_path, settings=settings, cases=cases)


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
    cases: list[dict] | None = None,
) -> EvalRun:
    settings = settings or get_settings()
    run_started = time.time()
    incr("eval_runs_total")
    run.status = EvalStatus.running
    run.summary_json = {"status": "running"}
    await db.flush()
    try:
        if cases is not None:
            cases = [normalize_case(case) for case in cases]
        else:
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

    # A retried job must not duplicate (eval_run_id, case_id) rows.
    await db.execute(delete(EvalRunCase).where(EvalRunCase.eval_run_id == run.id))
    min_recall = float(getattr(settings, "eval_pass_min_evidence_recall", EVAL_PASS_MIN_EVIDENCE_RECALL))
    max_forbidden = int(getattr(settings, "eval_pass_max_forbidden_hits", EVAL_PASS_MAX_FORBIDDEN_HITS))
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
            case_result = evaluate_case_output(
                case,
                evidence_paths,
                answer_payload,
                [str(item.get("source_type") or "unknown_text") for item in investigation.evidence],
            )
            usage = _llm_usage(investigation)
            case_result.update(
                {
                    **usage,
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
                "query_class": str(case.get("query_class") or classify_query_intent(str(case.get("question", ""))).intent),
                "expected_source_types": case.get("expected_source_types", []),
                "observed_source_types": [],
                "source_type_hit_rate": 0.0,
                "wrong_source_type_hits": [],
                "latency_ms": int((time.time() - started) * 1000),
                "error": _safe_error(exc),
            }
        case_result["passed"] = (
            not case_result.get("error")
            and float(case_result["evidence_recall"]) >= min_recall
            and len(case_result["forbidden_hits"]) <= max_forbidden
        )
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
    error_cases = sum(1 for result in results if result.get("error"))
    passed_cases = sum(1 for result in results if result["passed"])
    failed_cases = len(results) - passed_cases
    input_tokens = sum(int(result.get("input_tokens") or 0) for result in results)
    output_tokens = sum(int(result.get("output_tokens") or 0) for result in results)
    model_call_count = sum(int(result.get("model_call_count") or 0) for result in results)
    estimated_cost_usd = (
        input_tokens / 1000 * float(getattr(settings, "llm_input_cost_per_1k_tokens_usd", 0.0) or 0.0)
        + output_tokens / 1000 * float(getattr(settings, "llm_output_cost_per_1k_tokens_usd", 0.0) or 0.0)
    )
    zero_result_count = sum(1 for result in results if not result.get("retrieved_documents"))
    citation_count = sum(len(result.get("retrieved_documents") or []) for result in results)
    citation_rate = sum(1 for result in results if result.get("retrieved_documents")) / len(results) if results else 0.0
    avg_latency_ms = sum(int(result.get("latency_ms") or 0) for result in results) / len(results) if results else 0.0
    latency_values = [int(result.get("latency_ms") or 0) for result in results]
    avg_source_type_hit_rate = sum(float(result["source_type_hit_rate"]) for result in results) / len(results) if results else 0.0
    wrong_source_type_rate = sum(1 for result in results if result["wrong_source_type_hits"]) / len(results) if results else 0.0
    run.status = EvalStatus.completed
    run.summary_json = {
        "total_cases": len(results),
        "passed_cases": passed_cases,
        "failed_cases": failed_cases,
        "error_cases": error_cases,
        "pass_thresholds": {"min_evidence_recall": min_recall, "max_forbidden_hits": max_forbidden},
        "avg_evidence_recall": avg_recall,
        "avg_term_coverage": avg_term_coverage,
        "forbidden_hit_count": forbidden_hits,
        "avg_latency_ms": avg_latency_ms,
        "latency_p50_ms": _percentile(latency_values, 50),
        "latency_p95_ms": _percentile(latency_values, 95),
        "avg_source_type_hit_rate": avg_source_type_hit_rate,
        "wrong_source_type_rate": wrong_source_type_rate,
        "zero_result_count": zero_result_count,
        "citation_count": citation_count,
        "citation_rate": citation_rate,
        "model_call_count": model_call_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": round(estimated_cost_usd, 6),
        "rerank_gain_available": False,
        "rerank_gain_cases": 0,
        "retrieval_backend": settings.retrieval_backend,
        "embedding_model": settings.embedding_model,
        "vector_index_version": settings.vector_index_version,
        "reranker_model": settings.reranker_model or None,
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


def _llm_usage(investigation) -> dict:
    """Token usage reported by the answer path, when it exposes any."""
    debug = getattr(investigation, "debug", None) or {}
    usage = getattr(investigation, "usage", None) or debug.get("llm_usage") or debug.get("usage") or {}
    if not isinstance(usage, dict):
        return {"model_call_count": 0, "input_tokens": 0, "output_tokens": 0}
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    calls = int(usage.get("model_call_count") or usage.get("calls") or (1 if input_tokens or output_tokens else 0))
    return {"model_call_count": calls, "input_tokens": input_tokens, "output_tokens": output_tokens}


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
            case_result = evaluate_case_output(
                case,
                evidence_paths,
                answer_payload,
                [str(item.get("source_type") or "unknown_text") for item in payload.get("evidence", [])],
            )
            case_result["case_id"] = case["id"]
            results.append(case_result)
    summary = {
        "cases": len(results),
        "avg_recall": sum(result["evidence_recall"] for result in results) / len(results) if results else 0.0,
        "avg_term_coverage": sum(result["term_coverage"] for result in results) / len(results) if results else 0.0,
        "forbidden_hit_cases": sum(1 for result in results if result["forbidden_hits"]),
        "avg_source_type_hit_rate": sum(result["source_type_hit_rate"] for result in results) / len(results) if results else 0.0,
        "wrong_source_type_rate": sum(1 for result in results if result["wrong_source_type_hits"]) / len(results) if results else 0.0,
        "latency_p50_ms": _percentile([int(result.get("latency_ms") or 0) for result in results], 50),
        "latency_p95_ms": _percentile([int(result.get("latency_ms") or 0) for result in results], 95),
        "results": results,
    }
    return summary


def _percentile(values: list[int], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((percentile / 100) * (len(ordered) - 1))))
    return float(ordered[index])


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
