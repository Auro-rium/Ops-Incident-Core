"""Retrieval, rerank, and investigation paths with every remote and DB call stubbed."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from incidentops.config import settings as settings_module
from incidentops.config.settings import Settings
from incidentops.investigation import service as investigation_service
from incidentops.investigation.deploy_analyzer import analyze_deploy_diff
from incidentops.investigation.incident_matcher import find_previous_incidents
from incidentops.investigation.log_analyzer import analyze_logs
from incidentops.investigation.root_cause_selector import INSUFFICIENT_EVIDENCE_SUMMARY
from incidentops.retrieval import hybrid_search as hybrid_module
from incidentops.retrieval import reranker as reranker_module
from incidentops.retrieval.model_gateway import RemoteModelError

QUERY = "Why did GET /v1/orders slow down after deploy abc1234?"
LOOKUP_QUERY = "Where is handle_orders implemented?"


def _chunk(
    path: str = "svc/a.py",
    *,
    source_type: str = "code",
    chunk_type: str = "function",
    text: str = "def a(): pass",
    deploy_hash: str | None = None,
    timestamp: datetime | None = None,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        chunk_type=chunk_type,
        metadata_json={"source_type": source_type},
        document=SimpleNamespace(path=path),
        service_name=None,
        deploy_hash=deploy_hash,
        endpoint=None,
        section_title=None,
        start_line=1,
        end_line=2,
        timestamp_start=timestamp,
        timestamp_end=timestamp,
        text=text,
    )


def _settings(**overrides) -> Settings:
    defaults = {"_env_file": None, "rag_parallel_retrieval": False, "embedding_model": "local-hash"}
    return Settings(**{**defaults, **overrides})


def _results(*scores: float) -> list[dict]:
    return [{"chunk": _chunk(f"svc/{index}.py"), "fused_score": score} for index, score in enumerate(scores)]


def _paths(results: list[dict]) -> list[str]:
    return [result["chunk"].document.path for result in results]


# Reranking


def _rerank(monkeypatch, results, *, settings=None, top_k=10, model_name=""):
    monkeypatch.setattr(settings_module, "get_settings", lambda: settings or _settings())
    return asyncio.run(reranker_module.rerank_with_debug_async("q", results, model_name=model_name, top_k=top_k))


def test_rerank_disabled_orders_by_fused_score(monkeypatch):
    reranked, debug = _rerank(monkeypatch, _results(0.1, 0.9), settings=_settings(rag_rerank_mode="disabled"))

    assert debug["mode"] == "disabled"
    assert _paths(reranked) == ["svc/1.py", "svc/0.py"]


def test_rerank_without_results_is_skipped(monkeypatch):
    assert _rerank(monkeypatch, [])[1]["reason"] == "no_results"


def test_remote_rerank_keeps_reranked_head_before_fused_tail(monkeypatch):
    settings = _settings(rag_rerank_mode="always", rag_reranker_endpoint="https://rerank.invalid", rag_rerank_max_candidates=2)

    async def remote(query, documents):
        assert len(documents) == 2
        return [0.1, 0.9]

    monkeypatch.setattr(reranker_module, "remote_rerank", remote)

    reranked, debug = _rerank(monkeypatch, _results(0.5, 0.45, 0.2, 0.3), settings=settings)

    assert debug["mode"] == "remote_cross_encoder"
    assert _paths(reranked) == ["svc/1.py", "svc/0.py", "svc/3.py", "svc/2.py"]
    assert reranked[2].get("rerank_score") is None


def test_remote_rerank_failure_falls_back_to_fused_order(monkeypatch):
    settings = _settings(rag_rerank_mode="always", rag_reranker_endpoint="https://rerank.invalid")

    async def remote(query, documents):
        raise RemoteModelError("unavailable")

    monkeypatch.setattr(reranker_module, "remote_rerank", remote)

    reranked, debug = _rerank(monkeypatch, _results(0.2, 0.3), settings=settings)

    assert (debug["mode"], debug["reason"]) == ("fallback", "remote_model_failure")
    assert _paths(reranked) == ["svc/1.py", "svc/0.py"]


def test_conditional_remote_rerank_skips_decisive_results(monkeypatch):
    settings = _settings(rag_rerank_mode="conditional", rag_reranker_endpoint="https://rerank.invalid")

    async def remote(query, documents):
        raise AssertionError("decisive results must not call the remote reranker")

    monkeypatch.setattr(reranker_module, "remote_rerank", remote)

    reranked, debug = _rerank(monkeypatch, _results(0.9, 0.1), settings=settings)

    assert debug["mode"] == "heuristic"
    assert _paths(reranked)[0] == "svc/0.py"


# Hybrid search branches


def _stub_hybrid(monkeypatch, *, vector=None, lexical=None, metadata=None, settings=None) -> dict:
    calls = {"embed": 0, "vector": 0}

    def branch(value):
        async def run(*args, **kwargs):
            if isinstance(value, Exception):
                raise value
            return value or []

        return run

    async def embed(query, model):
        calls["embed"] += 1
        return [0.1, 0.2]

    vector_branch = branch(vector)

    async def counted_vector(*args, **kwargs):
        calls["vector"] += 1
        return await vector_branch(*args, **kwargs)

    async def no_cache(*args, **kwargs):
        return None

    async def known_services(db, project_id):
        return set()

    monkeypatch.setattr(hybrid_module, "get_settings", lambda: settings or _settings())
    monkeypatch.setattr(hybrid_module, "get_project_json", no_cache)
    monkeypatch.setattr(hybrid_module, "set_project_json", no_cache)
    monkeypatch.setattr(hybrid_module, "known_service_names", known_services)
    monkeypatch.setattr(hybrid_module, "embed_query_async", embed)
    monkeypatch.setattr(hybrid_module, "vector_search", counted_vector)
    monkeypatch.setattr(hybrid_module, "lexical_search", branch(lexical))
    monkeypatch.setattr(hybrid_module, "metadata_search", branch(metadata))
    monkeypatch.setattr(hybrid_module, "graph_search", branch([]))
    return calls


def _search(query: str = QUERY, top_k: int = 5):
    return asyncio.run(hybrid_module.hybrid_search_with_debug(None, uuid.uuid4(), query, top_k=top_k))


def test_failed_branch_reduces_recall_without_failing_the_search(monkeypatch):
    chunk = _chunk()
    _stub_hybrid(monkeypatch, vector=RuntimeError("qdrant down"), lexical=[{"chunk": chunk, "score": 1.0}])

    results, debug = _search()

    assert [result["chunk"] for result in results] == [chunk]
    assert debug["retrieval_branch_failures"] == {"vector": "RuntimeError"}
    assert debug["vector_candidates_count"] == 0


def test_exact_lookup_skips_embedding_when_other_branches_hit(monkeypatch):
    calls = _stub_hybrid(monkeypatch, lexical=[{"chunk": _chunk(), "score": 1.0}])

    _, debug = _search(LOOKUP_QUERY)

    assert calls == {"embed": 0, "vector": 0}
    assert "vector_fallback" not in debug["retrieval_branch_latencies"]


def test_exact_lookup_falls_back_to_vector_when_other_branches_are_empty(monkeypatch):
    chunk = _chunk()
    calls = _stub_hybrid(monkeypatch, vector=[{"chunk": chunk, "score": 0.5}])

    results, debug = _search(LOOKUP_QUERY)

    assert calls == {"embed": 1, "vector": 1}
    assert [result["chunk"] for result in results] == [chunk]
    assert debug["retrieval_branch_latencies"]["vector_fallback"] == 1


def test_vector_fallback_failure_is_reported_not_raised(monkeypatch):
    _stub_hybrid(monkeypatch, vector=RuntimeError("qdrant down"))

    results, debug = _search(LOOKUP_QUERY)

    assert results == []
    assert debug["retrieval_branch_failures"]["vector_fallback"] == "RuntimeError"


def test_candidates_found_by_several_branches_rank_first_and_rest_are_reported(monkeypatch):
    shared, vector_only, lexical_only = _chunk("shared.py"), _chunk("vector.py"), _chunk("lexical.py")
    _stub_hybrid(
        monkeypatch,
        vector=[{"chunk": vector_only, "score": 0.9}, {"chunk": shared, "score": 0.8}],
        lexical=[{"chunk": lexical_only, "score": 0.9}, {"chunk": shared, "score": 0.8}],
    )

    results, debug = _search(top_k=1)

    assert results[0]["chunk"] is shared
    assert debug["merged_candidates_count"] == 3
    assert len(debug["top_rejected"]) == 2


def test_deploy_hash_in_query_boosts_matching_chunk(monkeypatch):
    matching, other = _chunk("deploy.json", deploy_hash="abc1234"), _chunk("other.py")
    _stub_hybrid(monkeypatch, lexical=[{"chunk": other, "score": 0.9}, {"chunk": matching, "score": 0.1}])

    results, _ = _search()

    assert results[0]["chunk"] is matching
    assert "deploy_hash_match" in results[0]["metadata_boost_reasons"]


# Evidence analyzers


def test_analyze_logs_counts_only_log_evidence():
    findings = analyze_logs(
        [
            {
                "source_type": "logs",
                "service_name": "orders",
                "timestamp_start": datetime(2026, 5, 1, tzinfo=timezone.utc),
                "text": "ERROR request took 1500ms and timed out",
            },
            {"source_type": "code", "text": "ERROR 9000ms timed out"},
        ]
    )

    assert findings["slow_requests"] == 1
    assert findings["max_latency_ms"] == 1500
    assert findings["errors"] == 1
    assert findings["timeouts"] == 1
    assert findings["timestamped_logs"] == 1
    assert findings["services"] == ["orders"]


def test_analyze_deploy_diff_only_counts_the_requested_hash():
    evidence = [
        {"source_type": "deploy", "text": "deploy abc1234 added blocking sleep", "section_title": "a.py"},
        {"source_type": "deploy", "text": "deploy fff9999", "section_title": "b.py"},
        {"source_type": "code", "text": "abc1234"},
    ]

    findings = analyze_deploy_diff(evidence, "abc1234")

    assert findings["diff_count"] == 1
    assert findings["changed_files"] == ["a.py"]
    assert findings["summary_points"]
    assert analyze_deploy_diff(evidence, None)["diff_count"] == 2


def test_previous_incidents_rank_matching_service_and_symptom_first():
    matches = find_previous_incidents(
        [
            {"source_type": "incident", "chunk_id": "weak", "text": "unrelated outage"},
            {"source_type": "incident", "chunk_id": "strong", "text": "orders latency root cause"},
            {"source_type": "logs", "chunk_id": "log", "text": "orders latency"},
        ],
        {"service_name": "orders", "symptom": "latency"},
    )

    assert [match["chunk_id"] for match in matches] == ["strong", "weak"]


# Investigation end to end


def _investigate(monkeypatch, chunks: list, query: str = QUERY):
    async def known_services(db, project_id):
        return set()

    async def search(db, project_id, query, top_k, filters):
        return [{"chunk": chunk, "fused_score": 0.5} for chunk in chunks]

    async def rerank(query, results, model_name, top_k):
        return results[:top_k], {"mode": "disabled"}

    monkeypatch.setattr(investigation_service, "known_service_names", known_services)
    monkeypatch.setattr(investigation_service, "hybrid_search", search)
    monkeypatch.setattr(investigation_service, "rerank_with_debug_async", rerank)
    result, _ = asyncio.run(investigation_service.investigate(None, uuid.uuid4(), query, 8, "", filters=None))
    return result


TIMESTAMP = datetime(2026, 5, 1, 10, 5, tzinfo=timezone.utc)


def _slow_log():
    return _chunk(
        "logs/orders.log", source_type="logs", chunk_type="log_window",
        text="GET /v1/orders took 1500ms", timestamp=TIMESTAMP,
    )


def _deploy():
    return _chunk(
        "deploys/history.json", source_type="deploy", chunk_type="deploy_commit",
        text="deploy abc1234 changed the orders handler", deploy_hash="abc1234", timestamp=TIMESTAMP,
    )


def test_investigate_high_confidence_keeps_hypothesis_and_states_assessment(monkeypatch):
    result = _investigate(monkeypatch, [_slow_log(), _deploy(), _chunk("svc/orders.py")])

    assert result.confidence == "high"
    assert result.likely_root_cause.confidence == "high"
    assert result.likely_root_cause is not result.hypotheses[0]
    assert result.hypotheses[0].confidence == "medium"
    assert "recent deploy" in result.likely_root_cause.summary
    assert "does not support" not in result.answer
    assert "deployment history or code diff not found" not in result.missing_data
    assert result.suggested_fix


def test_investigate_weak_hypothesis_is_low_and_generated_list_is_untouched(monkeypatch):
    error_log = _chunk(
        "logs/orders.log", source_type="logs", chunk_type="log_window",
        text="ERROR GET /v1/orders failed", timestamp=TIMESTAMP,
    )

    result = _investigate(monkeypatch, [error_log, _chunk("svc/orders.py"), _chunk("docs/run.md", source_type="runbook")])

    assert result.confidence == "low"
    assert result.likely_root_cause.confidence == "low"
    assert result.likely_root_cause.summary == INSUFFICIENT_EVIDENCE_SUMMARY
    assert result.hypotheses[0].hypothesis_id == "error_path_regression"
    assert result.hypotheses[0].summary != INSUFFICIENT_EVIDENCE_SUMMARY
    assert result.hypotheses[0].confidence == "medium"
    assert "does not support" in result.answer
    assert result.suggested_fix is None


def test_investigate_without_logs_reports_missing_logs_not_timestamps(monkeypatch):
    result = _investigate(monkeypatch, [_deploy(), _chunk("svc/orders.py")])

    assert result.confidence == "low"
    assert "no runtime logs found" in result.missing_data
    assert "logs missing timestamps" not in result.missing_data


def test_investigate_without_evidence_is_honest(monkeypatch):
    result = _investigate(monkeypatch, [])

    assert result.confidence == "low"
    assert result.answer == "No supporting evidence was retrieved for this question."


@pytest.mark.parametrize("chunks", [[], [_chunk("svc/orders.py")]])
def test_investigate_lookup_query_is_redirected_to_evidence_search(monkeypatch, chunks):
    result = _investigate(monkeypatch, chunks, query=LOOKUP_QUERY)

    assert result.investigation_supported is False
    assert result.confidence == "low"
    assert "cited evidence lookup" in result.likely_root_cause.summary
