"""
API request / response schemas.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    database: str
    vector_index: str


class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    demo_mode: bool = False


class CreateProjectResponse(BaseModel):
    project_id: UUID
    name: str
    created_at: str


class IngestRequest(BaseModel):
    path: str = Field(..., min_length=1)


class SkippedFileInfo(BaseModel):
    path: str
    reason: str
    size_bytes: int | None = None
    detail: str | None = None


class ParserErrorInfo(BaseModel):
    path: str
    error_summary: str


class SourceCoverageResponse(BaseModel):
    has_logs: bool
    has_code: bool
    has_deploys: bool
    has_incidents: bool
    has_runbooks: bool
    has_api_docs: bool = False
    source_type_counts: dict[str, int] = Field(default_factory=dict)
    chunk_type_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class IngestResponse(BaseModel):
    project_id: str
    documents_ingested: int
    total_files_seen: int
    files_ingested: int
    files_skipped: int
    chunks_created: int
    embedding_model: str
    duration_ms: int
    skipped_files: list[SkippedFileInfo] = Field(default_factory=list)
    parser_errors: list[ParserErrorInfo] = Field(default_factory=list)
    source_type_counts: dict[str, int] = Field(default_factory=dict)
    chunk_type_counts: dict[str, int] = Field(default_factory=dict)
    source_coverage: SourceCoverageResponse


class SourceCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    source_type: str = Field(..., min_length=1, max_length=64)
    sync_mode: str = Field(default="manual", min_length=1, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)


class SourceResponse(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    source_type: str
    status: str
    sync_mode: str
    last_sync_status: str | None = None
    last_sync_started_at: datetime | None = None
    last_sync_finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class CollectorRegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    environment: str | None = Field(default=None, max_length=256)
    version: str | None = Field(default=None, max_length=256)


class CollectorRegisterResponse(BaseModel):
    collector_id: UUID
    status: str


class SyncStartRequest(BaseModel):
    collector_id: UUID | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class SyncFinishRequest(BaseModel):
    status: str = Field(..., min_length=1, max_length=64)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)


class SyncResponse(BaseModel):
    sync_id: UUID
    project_id: UUID
    source_id: UUID
    collector_id: UUID | None = None
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    files_seen: int = 0
    documents_received: int = 0
    chunks_created: int = 0
    files_skipped: int = 0
    parser_errors: int = 0
    coverage: dict[str, Any] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None


class SyncStatusResponse(BaseModel):
    sync_id: UUID
    status: str


class CapabilitiesResponse(BaseModel):
    version: str
    features: dict[str, bool]
    limits: dict[str, int]
    endpoints: dict[str, str]


class RuntimeStatusResponse(BaseModel):
    app_env: str
    llm_provider: str
    embedding_backend: str
    retrieval_backend: str
    worker_mode: str
    rate_limit_backend: str
    mcp_enabled: bool
    azure_openai_configured: bool
    azure_ai_search_configured: bool
    local_fallback_active: bool
    chat_deployment: str | None = None
    embedding_deployment: str | None = None


class ReadinessLatestSync(BaseModel):
    sync_id: str | None = None
    status: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    documents_received: int = 0
    chunks_created: int = 0
    skipped_unchanged: int = 0
    parser_errors: int = 0
    duration_seconds: float | None = None


class ReadinessCoverage(BaseModel):
    has_code: bool
    has_docs: bool
    has_logs: bool
    has_deploys: bool
    has_runbooks: bool
    has_incidents: bool
    has_api_docs: bool
    has_configs: bool


class ReadinessCounts(BaseModel):
    documents: int = 0
    chunks: int = 0
    sources: int = 0
    successful_syncs: int = 0
    failed_syncs: int = 0


class ReadinessResponse(BaseModel):
    project_id: UUID
    score: int
    grade: str
    summary: str
    source_count: int
    collector_count: int
    latest_sync: ReadinessLatestSync
    coverage: ReadinessCoverage
    counts: ReadinessCounts
    source_type_counts: dict[str, int] = Field(default_factory=dict)
    answerable_questions: list[str] = Field(default_factory=list)
    weak_questions: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    observability_gaps: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    suggested_questions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    readiness_generated_at: datetime
    generated_at: datetime


class BatchDocumentRequest(BaseModel):
    external_id: str = Field(..., min_length=1, max_length=4096)
    path: str = Field(..., min_length=1, max_length=4096)
    source_type: str = Field(..., min_length=1, max_length=64)
    content: str
    content_hash: str = Field(..., min_length=1, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)
    size_bytes: int | None = None
    modified_at: datetime | None = None


class BatchIngestRequest(BaseModel):
    sync_id: UUID
    collector_id: UUID | None = None
    collector_version: str | None = Field(default=None, max_length=256)
    schema_version: str | None = Field(default=None, max_length=256)
    core_api_version: str | None = Field(default=None, max_length=256)
    documents: list[BatchDocumentRequest] = Field(default_factory=list)


class BatchIngestErrorResponse(BaseModel):
    external_id: str | None = None
    path: str | None = None
    code: str = "index_error"
    error: str
    message: str | None = None


class BatchIngestResponse(BaseModel):
    received: int
    created: int
    updated: int
    skipped_unchanged: int
    skipped_invalid: int = 0
    chunks_created: int
    errors: list[BatchIngestErrorResponse] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    embedding_backend: str | None = None
    warnings: list[str] = Field(default_factory=list)


class SearchRequest(BaseModel):
    project_id: UUID
    query: str = Field(..., min_length=1, max_length=4096)
    top_k: int = Field(default=10, ge=1, le=100)
    filters: dict[str, Any] | None = None
    debug: bool = False


class CitationInfo(BaseModel):
    label: str
    path: str
    lines: str
    chunk_id: str | None = None


class SearchHit(BaseModel):
    chunk_id: str
    rank: int
    source_type: str
    document_path: str
    service_name: str | None
    deploy_hash: str | None
    endpoint: str | None
    score: float
    text_preview: str
    citation: CitationInfo


class SearchResponse(BaseModel):
    query: str
    results: list[SearchHit]
    total: int
    latency_ms: int
    query_intent: str | None = None
    evidence_mix: dict[str, dict[str, int]] = Field(default_factory=dict)
    debug: dict[str, Any] | None = None


class AnswerRequest(BaseModel):
    project_id: UUID
    query: str = Field(..., min_length=1, max_length=4096)
    top_k: int = Field(default=12, ge=1, le=50)


class AnswerCitation(BaseModel):
    label: str
    document_path: str
    lines: str
    chunk_id: str | None = None


class AnswerBody(BaseModel):
    summary: str
    likely_root_cause: str
    confidence: str
    affected_services: list[str]
    reasoning: str | None = None
    suggested_fix: str | None = None
    unknowns: list[str] | None = None
    citations: list[AnswerCitation]


class EvidenceItem(BaseModel):
    chunk_id: str
    source_type: str
    document_path: str
    service_name: str | None
    deploy_hash: str | None
    score: float
    text_preview: str
    citation: CitationInfo


class AnswerResponse(BaseModel):
    question: str
    query_intent: str | None = None
    answer: AnswerBody | None = None
    message: str | None = None
    evidence: list[EvidenceItem]
    latency_ms: int
    warnings: list[str] = Field(default_factory=list)


class InvestigationRequest(BaseModel):
    project_id: UUID
    query: str = Field(..., min_length=1, max_length=4096)
    top_k: int = Field(default=12, ge=1, le=50)
    debug: bool = False


class InvestigationEntityResponse(BaseModel):
    service_name: str | None = None
    endpoint: str | None = None
    deploy_hash: str | None = None
    symptom: str | None = None
    time_window: dict[str, str] | None = None


class TimelineEventResponse(BaseModel):
    ts: str | None = None
    event_type: str
    summary: str
    evidence_chunk_ids: list[str] = Field(default_factory=list)


class HypothesisResponse(BaseModel):
    hypothesis_id: str
    summary: str
    score: float
    confidence: str
    evidence_chunk_ids: list[str]
    supporting_reasons: list[str]
    contradicting_reasons: list[str] = Field(default_factory=list)


class RootCauseResponse(BaseModel):
    hypothesis_id: str | None = None
    summary: str
    confidence: str
    evidence_chunk_ids: list[str]


class InvestigationResponse(BaseModel):
    question: str
    task_type: str
    query_intent: str
    investigation_supported: bool = True
    entities: InvestigationEntityResponse
    timeline: list[TimelineEventResponse]
    hypotheses: list[HypothesisResponse]
    likely_root_cause: RootCauseResponse
    confidence: str
    confidence_reasons: list[str] = Field(default_factory=list)
    affected_services: list[str]
    suggested_fix: str | None = None
    citations: list[CitationInfo]
    missing_data: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    latency_ms: int
    debug: dict[str, Any] | None = None


class RunCreateRequest(BaseModel):
    project_id: UUID
    query: str = Field(..., min_length=1, max_length=4096)
    top_k: int = Field(default=12, ge=1, le=50)
    create_issue_draft: bool = True


class RunResponse(BaseModel):
    run_id: UUID
    project_id: UUID
    status: str
    task_type: str | None = None
    risk_level: str | None = None
    pending_approval: bool = False
    final_answer: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class RunEventResponse(BaseModel):
    sequence_no: int
    event_type: str
    node_name: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ApprovalDecisionRequest(BaseModel):
    rationale: str | None = Field(default=None, max_length=2000)


class ApprovalDecisionResponse(BaseModel):
    run_id: UUID
    status: str
    rationale: str | None = None


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=3, max_length=256)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: dict[str, Any]


class MetricsSummaryResponse(BaseModel):
    counters: dict[str, int | float]
    latencies_ms: dict[str, float]
    latency_counts: dict[str, int] = Field(default_factory=dict)


class EvalRunRequest(BaseModel):
    project_id: UUID
    top_k: int = Field(default=10, ge=1, le=50)
    cases_path: str | None = Field(default=None, min_length=1, max_length=4096)


class EvalRunResponse(BaseModel):
    eval_run_id: UUID
    project_id: UUID
    status: str
    summary: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime | None = None
