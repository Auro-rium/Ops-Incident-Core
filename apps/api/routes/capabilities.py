from __future__ import annotations

from fastapi import APIRouter, Depends

from apps.api.deps import get_settings_dep
from incidentops.config.settings import Settings
from incidentops.schemas.api import CapabilitiesResponse

router = APIRouter(prefix="/v1", tags=["Capabilities"])

CORE_API_VERSION = "0.5.0"


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def get_capabilities(settings: Settings = Depends(get_settings_dep)) -> CapabilitiesResponse:
    return CapabilitiesResponse(
        version=CORE_API_VERSION,
        features={
            "sync_tracking": True,
            "collector_registration": True,
            "batch_ingest": True,
            "search": True,
            "investigate": True,
            "runs": True,
        },
        limits={
            "max_batch_size": settings.max_batch_bytes,
            "max_documents_per_batch": settings.max_documents_per_batch,
            "max_document_bytes": settings.max_document_bytes,
            "max_batch_bytes": settings.max_batch_bytes,
        },
        endpoints={
            "register_collector": "/v1/projects/{project_id}/collectors/register",
            "register_source": "/v1/projects/{project_id}/sources",
            "create_sync": "/v1/sources/{source_id}/syncs/start",
            "batch_upload": "/v1/sources/{source_id}/documents/batch",
            "update_sync": "/v1/sources/{source_id}/syncs/{sync_id}/finish",
            "search": "/v1/search",
            "investigate": "/v1/investigate",
            "runs": "/v1/runs",
            "run_status": "/v1/runs/{run_id}",
            "run_events": "/v1/runs/{run_id}/events",
        },
    )
