"""
Centralized application settings.
"""

from __future__ import annotations

import socket
from urllib.parse import quote

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_env: str = "local"
    db_create_all: bool = False
    db_require_migrations: bool = True

    database_url: str = "postgresql+asyncpg://incidentops:incidentops@localhost:5432/incidentops"

    cloud_provider: str = "auto"
    embedding_model: str = "local-hash-v1"
    embedding_dim: int = 384
    retrieval_backend: str = "qdrant"
    vector_index_version: str = "current"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "incidentops_chunks"
    qdrant_timeout_seconds: float = 10.0
    rag_model_revision: str = ""
    rag_embedding_endpoint: str = ""
    rag_reranker_endpoint: str = ""
    rag_gpu_endpoint_required: bool = False
    rag_rerank_mode: str = "conditional"
    rag_rerank_max_candidates: int = 20
    rag_rerank_max_chars_per_candidate: int = 1600
    rag_rerank_max_query_chars: int = 2048
    rag_streaming_enabled: bool = False
    rag_shadow_percent: int = 0
    rag_candidate_multiplier: int = 3
    rag_parallel_retrieval: bool = True
    rag_branch_timeout_seconds: float = 2.0
    rag_rrf_k: int = 60
    rag_graph_weight: float = 0.10
    rag_async_indexing: bool = False
    rag_index_max_retries: int = 3
    rag_index_dispatch_batch_size: int = 50
    rag_cache_enabled: bool = True
    rag_cache_ttl_seconds: int = 3600
    rag_remote_timeout_seconds: int = 15
    rag_remote_api_key: str = ""
    rag_remote_auth_mode: str = "managed_identity"
    allow_local_model_loading: bool = False

    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o"
    llm_timeout_seconds: int = 60
    embedding_request_max_retries: int = 8
    embedding_request_initial_backoff_seconds: float = 1.0
    embedding_request_max_backoff_seconds: float = 20.0
    embedding_request_min_interval_seconds: float = 0.25
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_chat_deployment: str = ""
    azure_openai_embedding_deployment: str = ""
    require_azure_openai: bool = True

    vector_weight: float = 0.40
    lexical_weight: float = 0.30
    metadata_weight: float = 0.20
    default_top_k: int = 10
    reranker_model: str = ""

    host: str = "0.0.0.0"
    port: int = 8000

    # Legacy names are kept for env/API compatibility, but JWT_* is the
    # production auth configuration used by the application.
    auth_secret: str = "incidentops-dev-secret"
    auth_token_ttl_seconds: int = 3600
    jwt_secret: str = "incidentops-local-jwt-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "incidentops"
    jwt_audience: str = "incidentops-api"
    access_token_expire_minutes: int = 60
    allow_local_seed_admin: bool = True

    user_request_limit: int = 30
    project_ingestion_limit: int = 20
    collector_batch_request_limit: int = 2000
    rate_limit_enabled: bool = True
    rate_limit_backend: str = "memory"
    rate_limit_window_seconds: int = 60
    redis_url: str = "redis://localhost:6379/0"
    redis_host: str = ""
    redis_port: int = 6379
    redis_password: str = ""
    redis_ssl: bool = False
    redis_database: int = 0
    max_query_length: int = 4096
    max_top_k: int = 20
    max_retrieved_chunks: int = 20
    max_documents_per_batch: int = 100
    max_document_bytes: int = 2_000_000
    max_batch_bytes: int = 10_000_000
    max_chunks_per_document: int = 500
    max_metadata_bytes: int = 64_000
    max_external_id_length: int = 1024
    max_path_length: int = 2048
    max_context_tokens: int = 12000
    max_sync_diagnostics_bytes: int = 64_000
    graph_timeout_seconds: int = 120
    worker_mode: str = "inline"
    job_queue_backend: str = "inline"
    workflow_node_timeout_seconds: int = 60
    workflow_max_retries: int = 1
    workflow_run_timeout_seconds: int = 300
    eval_run_timeout_seconds: int = 600
    job_poll_interval_seconds: int = 2
    worker_concurrency: int = 1
    job_queue_consumer_group: str = "incidentops-workers"
    job_queue_consumer_name: str = Field(default_factory=lambda: f"core-worker-{socket.gethostname()}")
    job_queue_claim_idle_ms: int = 60000
    worker_job_max_retries: int = 2
    operational_agent_timeout_seconds: int = 180
    observer_sync_window: int = 20
    observer_parser_error_rate_threshold: float = 0.10
    observer_min_eval_recall: float = 0.60
    observer_max_wrong_source_type_rate: float = 0.25
    observer_eval_p95_latency_ms: int = 1500
    logging_aggregate_event_limit: int = 500
    enable_otel: bool = False
    otel_service_name: str = "incidentops-core"
    otel_exporter_otlp_endpoint: str = ""
    metrics_backend: str = "memory"
    metrics_public: bool = False
    local_ingest_enabled: bool = Field(
        True,
        validation_alias=AliasChoices("LOCAL_INGEST_ENABLED", "ENABLE_LOCAL_INGEST"),
    )
    local_ingest_allowed_roots: str = ".,/tmp"
    eval_cases_allowed_roots: str = ".,/tmp"
    max_eval_cases_bytes: int = 1_000_000
    demo_mode_public: bool = False
    allow_demo_project_bypass: bool = True
    max_ingest_file_bytes: int = 2_000_000
    max_chunk_tokens: int = 512
    answer_max_evidence_chunks: int = 8
    answer_max_chars_per_chunk: int = 1600
    answer_max_total_chars: int = 10000
    direct_answer_max_chars_per_chunk: int = 900
    direct_answer_max_total_chars: int = 4500
    supported_extensions: str = ".md,.txt,.log,.json,.yaml,.yml,.toml,.ini,.py,.js,.ts,.jsx,.tsx,.go,.java,.proto,.patch,.diff"
    cors_allow_origins: str = Field(
        "*",
        validation_alias=AliasChoices("CORS_ALLOW_ORIGINS", "CORS_ORIGINS"),
    )
    allow_wildcard_cors: bool = True
    mcp_enabled: bool = False
    mcp_core_api_url: str = Field(
        "http://127.0.0.1:8000",
        validation_alias=AliasChoices("MCP_CORE_API_URL", "INCIDENTOPS_CORE_API_URL"),
    )
    mcp_token: str = Field("", validation_alias=AliasChoices("MCP_TOKEN", "INCIDENTOPS_MCP_TOKEN"))
    mcp_transport: str = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8080
    mcp_path: str = "/mcp"

    @property
    def llm_available(self) -> bool:
        return (
            self.azure_openai_configured
            or (not self.is_production_like and bool(self.llm_api_key))
        )

    @property
    def effective_cloud_provider(self) -> str:
        configured = self.cloud_provider.strip().lower()
        if configured != "auto":
            return configured
        if self.azure_openai_configured or self.azure_openai_embeddings_configured:
            return "azure"
        if self.is_production_like and self.require_azure_openai:
            return "azure"
        return "local"

    @property
    def azure_openai_configured(self) -> bool:
        azure_key = self.azure_openai_api_key.strip()
        return bool(
            self.azure_openai_endpoint
            and azure_key
            and azure_key != "disabled"
            and self.azure_openai_chat_deployment
        )

    @property
    def azure_openai_embeddings_configured(self) -> bool:
        azure_key = self.azure_openai_api_key.strip()
        return bool(
            self.azure_openai_endpoint
            and azure_key
            and azure_key != "disabled"
            and self.azure_openai_embedding_deployment
        )

    @property
    def cloud_embeddings_configured(self) -> bool:
        if self.effective_cloud_provider == "azure":
            return self.azure_openai_embeddings_configured or self.gpu_rag_configured
        return self.embedding_model.startswith("local-hash") and not self.is_production_like

    @property
    def gpu_rag_configured(self) -> bool:
        auth_configured = (
            self.rag_remote_auth_mode == "managed_identity"
            or bool(self.rag_remote_api_key.strip())
        )
        return bool(
            self.rag_embedding_endpoint.strip()
            and self.rag_reranker_endpoint.strip()
            and auth_configured
            and self.embedding_model.startswith("azure-ml")
        )

    @property
    def supported_extensions_set(self) -> set[str]:
        return {item.strip().lower() for item in self.supported_extensions.split(",") if item.strip()}

    @property
    def resolved_redis_url(self) -> str:
        if self.redis_host.strip() and self.redis_password.strip():
            scheme = "rediss" if self.redis_ssl else "redis"
            password = quote(self.redis_password, safe="")
            return f"{scheme}://:{password}@{self.redis_host.strip()}:{self.redis_port}/{self.redis_database}"
        return self.redis_url

    @property
    def normalized_app_env(self) -> str:
        return self.app_env.strip().lower()

    @property
    def is_production_like(self) -> bool:
        return self.normalized_app_env in {"staging", "production"}

    @property
    def cors_origins_list(self) -> list[str]:
        return [item.strip() for item in self.cors_allow_origins.split(",") if item.strip()]


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
