from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from incidentops.config.settings import Settings
from incidentops.retrieval import vector_store
from incidentops.retrieval.vector_store import QdrantVectorStore, VectorStoreError


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise vector_store.httpx.HTTPStatusError("failed", request=None, response=None)


class _RecordingClient:
    requests: list[tuple[str, str, dict | None]] = []
    collection_dim = 4

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, *, headers):
        self.requests.append(("GET", url, None))
        return _Response(200, {"result": {"config": {"params": {"vectors": {"size": self.collection_dim}}}}})

    async def put(self, url, *, headers, json):
        self.requests.append(("PUT", url, json))
        return _Response(200, {"result": {}})

    async def post(self, url, *, headers, json):
        self.requests.append(("POST", url, json))
        return _Response(200, {"result": {}})


@pytest.fixture
def recording_client(monkeypatch):
    _RecordingClient.requests = []
    vector_store.reset_collection_cache()
    monkeypatch.setattr(vector_store.httpx, "AsyncClient", lambda **_kwargs: _RecordingClient())
    yield _RecordingClient
    vector_store.reset_collection_cache()


def _store() -> QdrantVectorStore:
    return QdrantVectorStore(
        Settings(
            qdrant_url="https://qdrant.example.test",
            qdrant_collection="incidentops_chunks",
            vector_index_version="v-test",
        )
    )


def _point(dim: int) -> dict:
    return {"id": str(uuid.uuid4()), "vector": [0.1] * dim, "payload": {}}


@pytest.mark.asyncio
async def test_collection_setup_is_cached_per_process_and_collection(recording_client):
    store = _store()

    await store.upsert([_point(4)], 4)
    await store.upsert([_point(4)], 4)

    lookups = [r for r in recording_client.requests if r[0] == "GET"]
    index_creations = [r for r in recording_client.requests if r[1].endswith("/index")]
    upserts = [r for r in recording_client.requests if "/points?wait=true" in r[1]]
    assert len(lookups) == 1
    assert len(index_creations) == len(vector_store._FILTER_FIELDS)
    assert len(upserts) == 2


@pytest.mark.asyncio
async def test_upsert_rejects_vectors_with_wrong_dimension(recording_client):
    with pytest.raises(VectorStoreError, match="dimension"):
        await _store().upsert([_point(4), _point(3)], 4)

    assert not [r for r in recording_client.requests if "/points?wait=true" in r[1]]


@pytest.mark.asyncio
async def test_delete_by_filter_is_scoped_to_active_index_version(recording_client):
    project_id = uuid.uuid4()

    await _store().delete_by_filter(project_id, document_id="doc-1")

    _, _, payload = recording_client.requests[-1]
    must = payload["filter"]["must"]
    assert {"key": "index_version", "match": {"value": "v-test"}} in must
    assert {"key": "project_id", "match": {"value": str(project_id)}} in must


@pytest.mark.asyncio
async def test_delete_by_filter_can_purge_every_index_version(recording_client):
    await _store().delete_by_filter(uuid.uuid4(), all_index_versions=True, source_id="s-1")

    _, _, payload = recording_client.requests[-1]
    assert "index_version" not in {item["key"] for item in payload["filter"]["must"]}


@pytest.mark.asyncio
async def test_delete_document_points_except_keeps_new_chunk_ids(recording_client):
    keep = [str(uuid.uuid4()), str(uuid.uuid4())]

    await _store().delete_document_points_except(uuid.uuid4(), "doc-1", keep)

    _, url, payload = recording_client.requests[-1]
    assert url.endswith("/points/delete?wait=true")
    assert payload["filter"]["must_not"] == [{"has_id": keep}]
    keys = {item["key"] for item in payload["filter"]["must"]}
    assert {"project_id", "document_id", "index_version"} <= keys


@pytest.mark.asyncio
async def test_delete_by_filter_matches_any_for_list_values(recording_client):
    await _store().delete_by_filter(uuid.uuid4(), all_index_versions=True, document_id=["a", "b"])

    _, _, payload = recording_client.requests[-1]
    assert {"key": "document_id", "match": {"any": ["a", "b"]}} in payload["filter"]["must"]


class _HtmlResponse(_Response):
    def json(self) -> dict:
        raise ValueError("not json")


@pytest.mark.asyncio
async def test_collection_lookup_with_non_json_reply_is_a_vector_store_error(recording_client, monkeypatch):
    async def html_get(self, url, *, headers):
        return _HtmlResponse(200)

    monkeypatch.setattr(_RecordingClient, "get", html_get)

    with pytest.raises(VectorStoreError):
        await _store().upsert([_point(4)], 4)


@pytest.mark.asyncio
async def test_search_with_non_json_reply_is_a_vector_store_error(recording_client, monkeypatch):
    async def html_post(self, url, *, headers, json):
        return _HtmlResponse(200)

    monkeypatch.setattr(_RecordingClient, "post", html_post)

    with pytest.raises(VectorStoreError):
        await _store().search(uuid.uuid4(), [0.1] * 4, 3)


def _search_filter(recording_client) -> list:
    _, _, payload = recording_client.requests[-1]
    return payload["filter"]["must"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/orders", "/orders/"])
async def test_search_endpoint_filter_matches_exact_or_path_prefix(recording_client, endpoint):
    await _store().search(uuid.uuid4(), [0.1] * 4, 3, {"endpoint": endpoint})

    must = _search_filter(recording_client)
    clause = next(item for item in must if "should" in item)
    assert {"key": "endpoint_prefixes", "match": {"value": "/orders"}} in clause["should"]
    assert {"key": "endpoint", "match": {"value": "/orders"}} in clause["should"]
    assert {"key": "endpoint", "match": {"value": endpoint}} in clause["should"]
    assert not [item for item in must if item.get("key") == "endpoint"]


@pytest.mark.asyncio
async def test_search_other_filters_stay_exact(recording_client):
    await _store().search(uuid.uuid4(), [0.1] * 4, 3, {"service_name": "orders"})

    assert {"key": "service_name", "match": {"value": "orders"}} in _search_filter(recording_client)


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("/orders/{id}", ["/", "/orders", "/orders/{id}"]),
        ("/orders/", ["/", "/orders"]),
        ("/", ["/"]),
        ("GET /orders", []),
        (None, []),
    ],
)
def test_endpoint_prefixes_mirror_the_lexical_filter(endpoint, expected):
    assert vector_store.endpoint_prefixes(endpoint) == expected


def test_endpoint_prefixes_do_not_match_a_sibling_path():
    assert "/orders" not in vector_store.endpoint_prefixes("/orders-v2/1")


def test_chunk_point_payload_carries_endpoint_prefixes():
    chunk = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        source_id=None,
        document_id=uuid.uuid4(),
        chunk_type="openapi_endpoint",
        service_name=None,
        endpoint="/orders/{id}",
        deploy_hash=None,
        metadata_json={},
    )

    point = vector_store.chunk_point(chunk, [0.1], index_version="v-test")

    assert point["payload"]["endpoint"] == "/orders/{id}"
    assert point["payload"]["endpoint_prefixes"] == ["/", "/orders", "/orders/{id}"]


def test_endpoint_prefixes_field_gets_a_payload_index():
    assert "endpoint_prefixes" in vector_store._FILTER_FIELDS
