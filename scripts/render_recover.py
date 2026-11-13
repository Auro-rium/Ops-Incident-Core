"""Restore the ephemeral Render vector index from stored chunks before serving.

Stop collector and ingestion writes before restarting and resume only after the
new instance is live. Render may route requests to an older instance meanwhile.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from incidentops.config.settings import get_settings
from incidentops.db.models import Chunk, Document
from incidentops.db.session import _get_session_factory
from incidentops.retrieval.embeddings import embed_texts_async
from incidentops.retrieval.vector_store import QdrantVectorStore, chunk_point

logger = logging.getLogger(__name__)
PAGE_SIZE = 16


async def replay_chunks(db, settings, store) -> int:
    """Replay every current chunk, including chunks whose source was removed."""
    after_id = None
    total = 0
    while True:
        statement = (
            select(Chunk, Document.path)
            .join(Document, Chunk.document_id == Document.id)
            .order_by(Chunk.id)
            .limit(PAGE_SIZE)
        )
        if after_id is not None:
            statement = statement.where(Chunk.id > after_id)
        rows = (await db.execute(statement)).all()
        if not rows:
            return total
        embeddings = await embed_texts_async(
            [chunk.text for chunk, _ in rows], model_name=settings.embedding_model
        )
        if len(embeddings) != len(rows):
            raise RuntimeError("Embedding backend returned an incomplete batch")
        points = [
            chunk_point(chunk, embedding, index_version=settings.vector_index_version, document_path=path)
            for (chunk, path), embedding in zip(rows, embeddings)
        ]
        await store.upsert(points, settings.embedding_dim)
        total += len(rows)
        after_id = rows[-1][0].id


async def main() -> None:
    settings = get_settings()
    if settings.embedding_model != "local-hash-v1":
        raise RuntimeError("Render recovery requires local-hash-v1; external providers are disabled")
    store = QdrantVectorStore(settings)
    for attempt in range(60):
        if await store.health():
            break
        if attempt == 59:
            raise RuntimeError("Qdrant did not become healthy")
        await asyncio.sleep(2)
    factory = _get_session_factory()
    async with factory() as db:
        total = await replay_chunks(db, settings, store)
    print(f"Render vector recovery finished: {total} chunks", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
