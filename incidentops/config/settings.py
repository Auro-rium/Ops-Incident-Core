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

    auth_secret: str = "incidentops-dev-secret"
    auth_token_ttl_seconds: int = 3600

    user_request_limit: int = 30
    project_ingestion_limit: int = 20
    max_query_length: int = 4096
    max_retrieved_chunks: int = 20
    max_context_tokens: int = 12000
    graph_timeout_seconds: int = 120
    demo_mode_public: bool = False
    max_ingest_file_bytes: int = 2_000_000
    max_chunk_tokens: int = 512
    supported_extensions: str = ".md,.txt,.log,.json,.yaml,.yml,.py,.patch,.diff"

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


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
