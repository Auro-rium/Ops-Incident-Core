from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from scripts import render_recover


@pytest.mark.asyncio
async def test_recovery_replays_every_page_with_stable_point_ids(monkeypatch):
    first = SimpleNamespace(id=uuid4(), text="first")
    second = SimpleNamespace(id=uuid4(), text="second")
    db = SimpleNamespace(execute=AsyncMock(side_effect=[
        SimpleNamespace(all=lambda: [(first, "one")]),
        SimpleNamespace(all=lambda: [(second, "two")]),
        SimpleNamespace(all=lambda: []),
    ]))
    store = SimpleNamespace(upsert=AsyncMock())
    settings = SimpleNamespace(embedding_model="local-hash-v1", embedding_dim=2, vector_index_version="v1")
    monkeypatch.setattr(render_recover, "embed_texts_async", AsyncMock(return_value=[[1.0, 0.0]]))
    monkeypatch.setattr(
        render_recover, "chunk_point",
        lambda chunk, vector, **kwargs: {"id": str(chunk.id), "vector": vector},
    )

    assert await render_recover.replay_chunks(db, settings, store) == 2
    assert [call.args[0][0]["id"] for call in store.upsert.await_args_list] == [
        str(first.id), str(second.id)
    ]


@pytest.mark.asyncio
async def test_recovery_rejects_incomplete_embedding_batch(monkeypatch):
    chunk = SimpleNamespace(id=uuid4(), text="first")
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(chunk, "one")])))
    store = SimpleNamespace(upsert=AsyncMock())
    settings = SimpleNamespace(embedding_model="local-hash-v1")
    monkeypatch.setattr(render_recover, "embed_texts_async", AsyncMock(return_value=[]))

    with pytest.raises(RuntimeError, match="incomplete batch"):
        await render_recover.replay_chunks(db, settings, store)
    store.upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_refuses_external_embedding_provider(monkeypatch):
    monkeypatch.setattr(render_recover, "get_settings", lambda: SimpleNamespace(embedding_model="remote"))
    with pytest.raises(RuntimeError, match="external providers are disabled"):
        await render_recover.main()
