"""
Centralized application settings.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    db_create_all: bool = False
    db_require_migrations: bool = True

    database_url: str = "postgresql+asyncpg://incidentops:incidentops@localhost:5432/incidentops"

    embedding_model: str = "local-hash-v1"
    embedding_dim: int = 384

    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o"
    llm_timeout_seconds: int = 60

    vector_weight: float = 0.45
    lexical_weight: float = 0.35
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
    rate_limit_enabled: bool = True
    rate_limit_backend: str = "memory"
    rate_limit_window_seconds: int = 60
    redis_url: str = "redis://localhost:6379/0"
    max_query_length: int = 4096
    max_top_k: int = 20
    max_retrieved_chunks: int = 20
    max_documents_per_batch: int = 100
    max_document_bytes: int = 2_000_000
    max_batch_bytes: int = 10_000_000
    max_context_tokens: int = 12000
    max_sync_diagnostics_bytes: int = 64_000
    graph_timeout_seconds: int = 120
    demo_mode_public: bool = False
    allow_demo_project_bypass: bool = True
    max_ingest_file_bytes: int = 2_000_000
    max_chunk_tokens: int = 512
    supported_extensions: str = ".md,.txt,.log,.json,.yaml,.yml,.py,.patch,.diff"
    cors_allow_origins: str = "*"
    allow_wildcard_cors: bool = True

    @property
    def llm_available(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def supported_extensions_set(self) -> set[str]:
        return {item.strip().lower() for item in self.supported_extensions.split(",") if item.strip()}

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
