from __future__ import annotations

from incidentops.config.settings import Settings

_DEFAULT_JWT_SECRETS = {
    "",
    "incidentops-dev-secret",
    "incidentops-local-jwt-secret-change-me",
    "change-me",
    "changeme",
}


def validate_startup_settings(settings: Settings) -> None:
    errors = production_settings_errors(settings)
    if errors:
        formatted = "; ".join(errors)
        raise RuntimeError(f"Unsafe production configuration: {formatted}")


def production_settings_errors(settings: Settings) -> list[str]:
    if not settings.is_production_like:
        return []

    errors: list[str] = []
    jwt_secret = settings.jwt_secret.strip()
    if jwt_secret in _DEFAULT_JWT_SECRETS or len(jwt_secret) < 32:
        errors.append("JWT_SECRET must be set to a strong non-default value")
    if settings.db_create_all:
        errors.append("DB_CREATE_ALL must be false in staging/production")
    if settings.demo_mode_public:
        errors.append("DEMO_MODE_PUBLIC must be false in staging/production")
    if settings.allow_demo_project_bypass:
        errors.append("ALLOW_DEMO_PROJECT_BYPASS must be false in staging/production")
    if settings.allow_local_seed_admin:
        errors.append("ALLOW_LOCAL_SEED_ADMIN must be false in staging/production")
    if settings.rate_limit_enabled and settings.rate_limit_backend == "memory":
        errors.append("RATE_LIMIT_BACKEND=memory is not allowed in staging/production")
    if settings.worker_mode != "queue":
        errors.append("WORKER_MODE=queue is required in staging/production")
    if settings.job_queue_backend != "redis":
        errors.append("JOB_QUEUE_BACKEND=redis is required in staging/production")
    if settings.job_queue_backend == "redis" and not settings.resolved_redis_url.strip():
        errors.append("REDIS_URL is required when JOB_QUEUE_BACKEND=redis")
    if settings.metrics_backend == "memory":
        errors.append("METRICS_BACKEND=memory is not allowed as the only metrics backend in staging/production")
    if settings.local_ingest_enabled:
        errors.append("LOCAL_INGEST_ENABLED=true is not allowed in staging/production")
    if settings.allow_local_model_loading:
        errors.append("ALLOW_LOCAL_MODEL_LOADING=true is not allowed in staging/production")
    if settings.cors_origins_list == ["*"] and not settings.allow_wildcard_cors:
        errors.append("CORS wildcard is disabled but CORS_ALLOW_ORIGINS=*")
    if settings.cors_origins_list == ["*"] and settings.allow_wildcard_cors:
        errors.append("CORS wildcard is not allowed in staging/production")
    provider = settings.effective_cloud_provider
    if provider != "azure":
        errors.append("CLOUD_PROVIDER must resolve to azure in staging/production")
    if provider == "azure":
        if settings.require_azure_openai and not settings.azure_openai_configured:
            errors.append(
                "Azure OpenAI/Foundry chat deployment is required in staging/production "
                "when REQUIRE_AZURE_OPENAI=true"
            )
        if (
            settings.require_azure_openai
            and not settings.azure_openai_embeddings_configured
            and not settings.gpu_rag_configured
        ):
            errors.append(
                "Azure OpenAI/Foundry embedding deployment is required in staging/production "
                "when REQUIRE_AZURE_OPENAI=true unless Azure ML GPU embeddings are configured"
            )
        if settings.require_azure_openai and not settings.embedding_model.startswith("azure-openai"):
            if not (settings.rag_gpu_endpoint_required and settings.embedding_model.startswith("azure-ml")):
                errors.append("EMBEDDING_MODEL must be azure-openai or azure-ml in staging/production")
        if settings.rag_gpu_endpoint_required and not settings.gpu_rag_configured:
            errors.append(
                "RAG_GPU_ENDPOINT_REQUIRED=true requires Azure ML embedding/reranker endpoints, "
                "configured remote authentication, and EMBEDDING_MODEL=azure-ml"
            )
    if settings.rag_gpu_endpoint_required and not settings.rag_async_indexing:
        errors.append("RAG_GPU_ENDPOINT_REQUIRED=true requires RAG_ASYNC_INDEXING=true")
    if settings.rag_gpu_endpoint_required and not settings.rag_model_revision.strip():
        errors.append("RAG_GPU_ENDPOINT_REQUIRED=true requires an immutable RAG_MODEL_REVISION")
    if settings.retrieval_backend != "qdrant":
        errors.append("RETRIEVAL_BACKEND=qdrant is required in staging/production")
    if not settings.qdrant_url.strip():
        errors.append("QDRANT_URL is required in staging/production")
    if (
        settings.reranker_model
        and not settings.rag_reranker_endpoint
        and settings.rag_rerank_mode != "disabled"
    ):
        errors.append("production reranking requires RAG_RERANKER_ENDPOINT or RAG_RERANK_MODE=disabled")
    if not 0 <= settings.rag_shadow_percent <= 100:
        errors.append("RAG_SHADOW_PERCENT must be between 0 and 100")
    if settings.rag_rerank_mode not in {"conditional", "always", "disabled"}:
        errors.append("RAG_RERANK_MODE must be conditional, always, or disabled")
    if settings.rag_cache_ttl_seconds < 1:
        errors.append("RAG_CACHE_TTL_SECONDS must be at least 1")
    return errors
