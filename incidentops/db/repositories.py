"""
Database repository helpers — CRUD operations for Phase 1 tables.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.db.models import Chunk, Document, Project, RetrievalResult, RetrievalRun, Source


# ── Projects ──────────────────────────────────

async def create_project(db: AsyncSession, name: str) -> Project:
    project = Project(name=name)
    db.add(project)
    await db.flush()
    return project


async def get_project(db: AsyncSession, project_id: uuid.UUID) -> Project | None:
    result = await db.execute(select(Project).where(Project.id == project_id))
    return result.scalar_one_or_none()


async def get_project_by_name(db: AsyncSession, name: str) -> Project | None:
    result = await db.execute(select(Project).where(Project.name == name))
    return result.scalar_one_or_none()


# ── Sources ───────────────────────────────────

async def create_source(
    db: AsyncSession,
    project_id: uuid.UUID,
    name: str,
    source_type: str,
    source_uri: str | None,
    version_hash: str | None = None,
) -> Source:
    source = Source(
        project_id=project_id,
        name=name,
        source_type=source_type,
        source_uri=source_uri,
        version_hash=version_hash,
    )
    db.add(source)
    await db.flush()
    return source


# ── Documents ─────────────────────────────────

async def create_document(
    db: AsyncSession,
    project_id: uuid.UUID,
    source_id: uuid.UUID | None,
    external_id: str | None,
    title: str | None,
    path: str,
    doc_type: str,
    source_type: str | None = None,
    checksum: str | None = None,
    content_hash: str | None = None,
) -> Document:
    doc = Document(
        project_id=project_id,
        source_id=source_id,
        external_id=external_id,
        title=title,
        path=path,
        doc_type=doc_type,
        source_type=source_type,
        checksum=checksum,
        content_hash=content_hash or checksum,
    )
    db.add(doc)
    await db.flush()
    return doc


# ── Chunks ────────────────────────────────────

async def bulk_insert_chunks(db: AsyncSession, chunks: list[dict]) -> int:
    """Insert a batch of chunk dicts. Returns count inserted."""
    if not chunks:
        return 0
    for chunk_data in chunks:
        chunk = Chunk(**chunk_data)
        db.add(chunk)
    await db.flush()
    return len(chunks)


# ── Search ────────────────────────────────────

async def lexical_search(
    db: AsyncSession,
    project_id: uuid.UUID,
    query_text: str,
    top_k: int = 20,
    filters: dict[str, Any] | None = None,
) -> list[dict]:
    """Full-text search using Postgres tsvector/tsquery."""
    tsquery = func.plainto_tsquery("english", query_text)
    rank = func.ts_rank_cd(Chunk.search_tsvector, tsquery)

    stmt = (
        select(Chunk, rank.label("score"))
        .where(Chunk.project_id == project_id)
        .where(Chunk.search_tsvector.op("@@")(tsquery))
    )

    if filters:
        if filters.get("service_name"):
            stmt = stmt.where(Chunk.service_name == filters["service_name"])
        if filters.get("deploy_hash"):
            stmt = stmt.where(Chunk.deploy_hash == filters["deploy_hash"])
        if filters.get("chunk_type"):
            stmt = stmt.where(Chunk.chunk_type == filters["chunk_type"])

    stmt = stmt.order_by(rank.desc()).limit(top_k)
    result = await db.execute(stmt)
    rows = result.all()
    return [{"chunk": row[0], "score": float(row[1])} for row in rows]


# ── Retrieval logging ─────────────────────────

async def log_retrieval_run(
    db: AsyncSession,
    project_id: uuid.UUID,
    query: str,
    rewritten_query: str | None,
    top_k: int,
    latency_ms: int,
    results: list[dict],
) -> RetrievalRun:
    run = RetrievalRun(
        project_id=project_id,
        query=query,
        rewritten_query=rewritten_query,
        top_k=top_k,
        latency_ms=latency_ms,
    )
    db.add(run)
    await db.flush()

    for i, r in enumerate(results):
        rr = RetrievalResult(
            retrieval_run_id=run.id,
            chunk_id=r["chunk_id"],
            vector_score=r.get("vector_score"),
            lexical_score=r.get("lexical_score"),
            fused_score=r.get("fused_score"),
            rerank_score=r.get("rerank_score"),
            rank=i + 1,
        )
        db.add(rr)
    await db.flush()
    return run
