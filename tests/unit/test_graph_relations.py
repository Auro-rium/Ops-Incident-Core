from __future__ import annotations

import uuid
from types import SimpleNamespace

from incidentops.retrieval.graph_relations import build_chunk_relations


def test_graph_relations_only_reflect_indexed_chunk_facts():
    project_id = uuid.uuid4()
    chunk = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=project_id,
        chunk_type="go_function",
        service_name="history",
        endpoint="GET /api/history",
        deploy_hash="abc1234",
        metadata_json={"source_type": "code", "package_name": "history", "symbol_name": "GetHistory"},
    )
    document = SimpleNamespace(path="service/history/handler.go")

    relations = build_chunk_relations(document, [chunk])

    assert {(relation["relation_type"], relation["from_key"], relation["to_key"]) for relation in relations} == {
        ("contains", "path:service/history/handler.go", f"chunk:{chunk.id}"),
        ("contains", "service:history", "path:service/history/handler.go"),
        ("contains", "package:history", "path:service/history/handler.go"),
        ("defines", "path:service/history/handler.go", "symbol:GetHistory"),
        ("implements", "path:service/history/handler.go", "endpoint:GET /api/history"),
        ("changed_by", "path:service/history/handler.go", "deploy:abc1234"),
    }
    assert all(relation["project_id"] == project_id for relation in relations)
