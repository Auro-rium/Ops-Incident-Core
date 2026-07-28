from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from incidentops.config.settings import Settings
from incidentops.retrieval.vector_store import QdrantVectorStore, _collection_dimension, chunk_point


class _Response:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("request failed")


class _Client:
    requests: list[tuple[str, str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, *, headers, json):
        self.requests.append(("POST", url, json))
        return _Response(
            200,
            {
                "result": {
                    "points": [
                        {"id": "f484c4e6-c074-4af0-925f-6e130c3c1be3", "score": 0.91},
                    ]
                }
            },
        )


def test_chunk_point_keeps_payload_bounded_and_citation_metadata():
    chunk = SimpleNamespace(
        id=uuid.UUID("f484c4e6-c074-4af0-925f-6e130c3c1be3"),
        project_id=uuid.UUID("a64f9731-a4f8-4f7d-a6df-4d66fdf39e70"),
        source_id=uuid.UUID("e33d8a66-f48a-4c3b-bca5-21c84cd1861d"),
        document_id=uuid.UUID("329af06b-4978-462b-a606-91854b3c2b0e"),
        chunk_type="go_function",
        service_name="history",
        endpoint=None,
        deploy_hash="abc1234",
        metadata_json={
            "source_type": "code",
            "language": "go",
            "symbol_name": "GetHistory",
            "package_name": "service/history",
        },
        text="func GetHistory() { secret content must not be in vector payload }",
    )

    point = chunk_point(chunk, [0.2, 0.8], index_version="current", document_path="service/history/api.go")

    assert point["id"] == str(chunk.id)
    assert point["vector"] == [0.2, 0.8]
    assert point["payload"]["path"] == "service/history/api.go"
    assert point["payload"]["symbol_name"] == "GetHistory"
    assert "text" not in point["payload"]
    assert "secret content" not in repr(point["payload"])


def test_collection_dimension_reads_qdrant_shape():
    assert _collection_dimension({"result": {"config": {"params": {"vectors": {"size": 1024}}}}}) == 1024
    assert _collection_dimension({"result": {}}) is None


@pytest.mark.asyncio
async def test_qdrant_search_scopes_project_and_active_index(monkeypatch):
    _Client.requests = []
    monkeypatch.setattr("incidentops.retrieval.vector_store.httpx.AsyncClient", lambda **_kwargs: _Client())
    project_id = uuid.UUID("a64f9731-a4f8-4f7d-a6df-4d66fdf39e70")
    store = QdrantVectorStore(
        Settings(
            qdrant_url="https://qdrant.example.test",
            qdrant_collection="incidentops_chunks",
            vector_index_version="current",
        )
    )

    hits = await store.search(project_id, [0.1, 0.9], 5, {"source_id": "source-1"})

    assert [(hit.chunk_id, hit.score) for hit in hits] == [
        (uuid.UUID("f484c4e6-c074-4af0-925f-6e130c3c1be3"), 0.91)
    ]
    _, _, payload = _Client.requests[0]
    must = payload["filter"]["must"]
    assert {item["key"] for item in must} == {"project_id", "index_version", "source_id"}
