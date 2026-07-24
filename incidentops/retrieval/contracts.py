"""Versioned contracts for remote RAG model and index publication jobs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ModelInput(BaseModel):
    id: str
    text: str = Field(min_length=1, max_length=12000)


class EmbeddingRequest(BaseModel):
    model_id: str
    index_version: str
    inputs: list[ModelInput] = Field(min_length=1, max_length=128)


class EmbeddingResponse(BaseModel):
    model_id: str
    model_revision: str | None = None
    dimension: int = Field(gt=0)
    vectors: list[list[float]]
    latency_ms: int = Field(ge=0)
    batch_size: int = Field(ge=0)


class RerankCandidate(BaseModel):
    id: str
    text: str = Field(min_length=1, max_length=12000)


class RerankRequest(BaseModel):
    model_id: str
    query: str = Field(min_length=1, max_length=4096)
    candidates: list[RerankCandidate] = Field(min_length=1, max_length=64)


class RerankResponse(BaseModel):
    model_id: str
    model_revision: str | None = None
    scores: list[float]
    latency_ms: int = Field(ge=0)


JobKind = Literal["index_document", "embed_chunks", "publish_index", "evaluate_retrieval"]


class RAGJob(BaseModel):
    job_id: str
    kind: JobKind
    project_id: str
    source_id: str | None = None
    document_id: str | None = None
    index_version: str
    attempt: int = Field(default=0, ge=0)

