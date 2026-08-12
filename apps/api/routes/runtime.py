from __future__ import annotations

from fastapi import APIRouter, Depends

from apps.api.deps import get_settings_dep, require_user
from incidentops.config.settings import Settings
from incidentops.db.models import User
from incidentops.schemas.api import RuntimeStatusResponse

router = APIRouter(prefix="/v1/runtime", tags=["Runtime"])


def build_runtime_status(settings: Settings) -> RuntimeStatusResponse:
    provider = settings.effective_cloud_provider
    if settings.embedding_model.startswith("azure-ml"):
        embedding_backend = "azure_ml_gpu"
    elif settings.embedding_model.startswith(("huggingface", "hf")):
        embedding_backend = "huggingface"
    elif settings.embedding_model.startswith("nvidia"):
        embedding_backend = "nvidia_nemotron"
    else:
        embedding_backend = "azure_openai" if settings.embedding_model.startswith("azure-openai") else settings.embedding_model
    if provider == "azure" and settings.azure_openai_configured:
        llm_provider = "azure_openai"
    elif provider == "nvidia" and settings.nvidia_chat_configured:
        llm_provider = "nvidia"
    else:
        llm_provider = "openai_compatible" if settings.llm_available else "none"
    cloud_embedding_ready = settings.cloud_embeddings_configured
    local_fallback_active = settings.is_production_like and (
        llm_provider not in {"azure_openai", "nvidia"}
        or not cloud_embedding_ready
        or settings.worker_mode != "queue"
        or settings.rate_limit_backend != "redis"
    )
    return RuntimeStatusResponse(
        app_env=settings.normalized_app_env,
        cloud_provider=provider,
        llm_provider=llm_provider,
        embedding_backend=embedding_backend,
        retrieval_backend=settings.retrieval_backend,
        vector_store_configured=bool(settings.qdrant_url.strip()),
        vector_collection=settings.qdrant_collection or None,
        worker_mode=settings.worker_mode,
        rate_limit_backend=settings.rate_limit_backend,
        mcp_enabled=settings.mcp_enabled or bool(settings.mcp_token.strip()) or settings.mcp_transport.strip().lower() in {"streamable-http", "sse"},
        azure_openai_configured=settings.azure_openai_configured,
        azure_ai_search_configured=False,
        local_fallback_active=local_fallback_active,
        chat_deployment=(
            settings.azure_openai_chat_deployment
            if provider == "azure" and settings.azure_openai_configured
            else settings.nvidia_chat_model
            if provider == "nvidia" and settings.nvidia_chat_configured
            else None
        ),
        embedding_deployment=(
            settings.hf_embedding_model
            if settings.embedding_model.startswith(("huggingface", "hf"))
            else settings.nvidia_embedding_model
            if settings.embedding_model.startswith("nvidia")
            else settings.azure_openai_embedding_deployment or None
        ),
        embedding_dimension=settings.embedding_dim,
        azure_openai_embeddings_configured=settings.azure_openai_embeddings_configured,
        nvidia_embeddings_configured=settings.nvidia_embeddings_configured,
        vector_index_version=settings.vector_index_version,
        rag_rerank_mode=settings.rag_rerank_mode,
        gpu_rag_configured=settings.gpu_rag_configured,
    )


@router.get("/status", response_model=RuntimeStatusResponse)
async def get_runtime_status(
    _user: User = Depends(require_user),
    settings: Settings = Depends(get_settings_dep),
) -> RuntimeStatusResponse:
    return build_runtime_status(settings)
