from __future__ import annotations

from fastapi import APIRouter, Depends

from apps.api.deps import get_settings_dep, require_user
from incidentops.config.settings import Settings
from incidentops.db.models import User
from incidentops.schemas.api import RuntimeStatusResponse

router = APIRouter(prefix="/v1/runtime", tags=["Runtime"])


def build_runtime_status(settings: Settings) -> RuntimeStatusResponse:
    if settings.embedding_model.startswith("azure-ml"):
        embedding_backend = "azure_ml_gpu"
    else:
        embedding_backend = "azure_openai" if settings.embedding_model.startswith("azure-openai") else settings.embedding_model
    llm_provider = "azure_openai" if settings.azure_openai_configured else ("openai_compatible" if settings.llm_available else "none")
    cloud_embedding_ready = settings.azure_openai_embeddings_configured or settings.gpu_rag_configured
    local_fallback_active = settings.is_production_like and (
        not settings.azure_openai_configured
        or not cloud_embedding_ready
        or settings.worker_mode != "queue"
        or settings.rate_limit_backend != "redis"
    )
    return RuntimeStatusResponse(
        app_env=settings.normalized_app_env,
        llm_provider=llm_provider,
        embedding_backend=embedding_backend,
        retrieval_backend="postgres_pgvector",
        worker_mode=settings.worker_mode,
        rate_limit_backend=settings.rate_limit_backend,
        mcp_enabled=settings.mcp_enabled or bool(settings.mcp_token.strip()) or settings.mcp_transport.strip().lower() in {"streamable-http", "sse"},
        azure_openai_configured=settings.azure_openai_configured,
        azure_ai_search_configured=False,
        local_fallback_active=local_fallback_active,
        chat_deployment=settings.azure_openai_chat_deployment or None,
        embedding_deployment=settings.azure_openai_embedding_deployment or None,
        rag_retrieval_version=settings.rag_retrieval_version,
        rag_index_version=settings.rag_index_version,
        rag_rerank_mode=settings.rag_rerank_mode,
        gpu_rag_configured=settings.gpu_rag_configured,
    )


@router.get("/status", response_model=RuntimeStatusResponse)
async def get_runtime_status(
    _user: User = Depends(require_user),
    settings: Settings = Depends(get_settings_dep),
) -> RuntimeStatusResponse:
    return build_runtime_status(settings)
