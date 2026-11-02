from __future__ import annotations

import asyncio
from types import SimpleNamespace
import uuid

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from incidentops.db.models import Chunk
from incidentops.investigation.entity_extractor import extract_entities
from incidentops.investigation.scope_resolver import resolve_scope
from incidentops.retrieval import graph_search as graph_search_module
from incidentops.retrieval import hybrid_search as hybrid_module
from incidentops.retrieval.hybrid_search import analyze_query
from incidentops.retrieval.lexical_search import apply_chunk_filters
from incidentops.retrieval.query_intent import classify_query_intent
from incidentops.retrieval.source_types import chunk_source_type


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_deploy_hash_ignores_plain_numbers_and_hex_words():
    info = analyze_query("Why did 123456 requests hit the facade and 20260501 batch?")

    assert info["deploy_hashes"] == []


def test_deploy_hash_accepts_keyword_and_mixed_hex_tokens():
    assert analyze_query("Why did latency increase after deploy abc1234?")["deploy_hashes"] == ["abc1234"]
    assert analyze_query("Is 9f8e7d6c the bad build?")["deploy_hashes"] == ["9f8e7d6c"]
    assert analyze_query("commit 1234567 broke it")["deploy_hashes"] == ["1234567"]


def test_extracted_deploy_hash_is_boost_not_hard_filter():
    entities = extract_entities("Why did GET /v1/orders slow down after deploy abc1234?")
    scope = resolve_scope(entities)

    assert entities.deploy_hash == "abc1234"
    assert "deploy_hash" not in scope["filters"]
    explicit = resolve_scope(entities, explicit_filters={"deploy_hash": "abc1234"})
    assert explicit["filters"]["deploy_hash"] == "abc1234"


def test_services_match_only_known_project_services():
    query = "Why is the checkout service slow for history lookups?"

    assert analyze_query(query)["services"] == []
    assert analyze_query(query, known_services={"checkout", "payments"})["services"] == ["checkout"]
    assert analyze_query("orders-api is failing", known_services={"Orders-API"})["services"] == ["orders-api"]


def test_known_service_names_are_cached_per_project(monkeypatch):
    cache: dict[str, dict] = {}

    async def fake_get(namespace, project_id, value):
        return cache.get(f"{namespace}:{project_id}:{value}")

    async def fake_set(namespace, project_id, value, payload):
        cache[f"{namespace}:{project_id}:{value}"] = payload

    class _Result:
        def scalars(self):
            return SimpleNamespace(all=lambda: ["Checkout", None, "payments"])

    class _Db:
        calls = 0

        async def execute(self, stmt):
            _Db.calls += 1
            return _Result()

    monkeypatch.setattr(hybrid_module, "get_project_json", fake_get)
    monkeypatch.setattr(hybrid_module, "set_project_json", fake_set)
    project_id = uuid.uuid4()
    db = _Db()

    first = asyncio.run(hybrid_module.known_service_names(db, project_id))
    second = asyncio.run(hybrid_module.known_service_names(db, project_id))

    assert first == {"checkout", "payments"}
    assert second == first
    assert _Db.calls == 1


def test_endpoint_filter_uses_prefix_template_matching():
    sql = _sql(apply_chunk_filters(select(Chunk), {"endpoint": "/orders"}))

    assert "chunks.endpoint = '/orders'" in sql
    assert "LIKE '/orders/%%'" in sql or "LIKE '/orders/%'" in sql


def test_endpoint_filter_escapes_like_wildcards():
    compiled = apply_chunk_filters(select(Chunk), {"endpoint": "/user_profile/"}).compile(dialect=postgresql.dialect())

    assert "/user\\_profile/%" in compiled.params.values()
    assert "ESCAPE" in str(compiled)


def test_graph_search_applies_request_filters():
    captured = {}

    class _Result:
        def all(self):
            return []

    class _Db:
        async def execute(self, stmt):
            captured["sql"] = _sql(stmt)
            return _Result()

    asyncio.run(
        graph_search_module.graph_search(
            _Db(),
            uuid.uuid4(),
            {"services": ["checkout"], "endpoints": [], "query_terms": ["checkout"]},
            top_k=5,
            filters={"service_name": "checkout"},
        )
    )

    assert "chunks.service_name = 'checkout'" in captured["sql"]


def test_hybrid_graph_branch_receives_filters(monkeypatch):
    seen = {}

    async def fake_graph(db, project_id, query_info, *, top_k, filters=None):
        seen["filters"] = filters
        return []

    async def empty(*args, **kwargs):
        return []

    monkeypatch.setattr(hybrid_module, "graph_search", fake_graph)
    monkeypatch.setattr(hybrid_module, "lexical_search", empty)
    monkeypatch.setattr(hybrid_module, "metadata_search", empty)
    intent = classify_query_intent("Which documentation files reference service architecture?")

    asyncio.run(
        hybrid_module._retrieve_branches(
            None,
            uuid.uuid4(),
            "architecture",
            [],
            {"services": [], "endpoints": [], "query_terms": ["architecture"], "deploy_hashes": []},
            intent,
            10,
            {"service_name": "checkout"},
            parallel=False,
            timeout_seconds=1.0,
            branch_latencies={},
        )
    )

    assert seen["filters"] == {"service_name": "checkout"}


def test_investigate_alone_does_not_force_runtime_incident():
    assert classify_query_intent("Investigate the service architecture overview").intent == "architecture"
    assert classify_query_intent("Help me investigate where the retry handler is implemented").intent == "code_location"
    assert classify_query_intent("Investigate the timeout errors in checkout").intent == "runtime_incident"


def test_python_module_chunks_map_to_code():
    chunk = SimpleNamespace(chunk_type="python_module", metadata_json={}, document=SimpleNamespace(path="app/x.py"))

    assert chunk_source_type(chunk) == "code"
