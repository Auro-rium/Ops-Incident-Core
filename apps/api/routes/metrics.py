from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from apps.api.deps import get_current_user, get_settings_dep
from incidentops.config.settings import Settings
from incidentops.observability.metrics import prometheus_text, summary
from incidentops.schemas.api import MetricsSummaryResponse

router = APIRouter(tags=["Metrics"])


async def require_metrics_access(
    settings: Settings = Depends(get_settings_dep),
    user=Depends(get_current_user),
) -> None:
    if settings.metrics_public:
        return
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


@router.get("/v1/metrics/summary", response_model=MetricsSummaryResponse)
async def metrics_summary(_: None = Depends(require_metrics_access)):
    data = summary()
    return MetricsSummaryResponse(**data)


@router.get("/metrics")
async def prometheus_metrics(_: None = Depends(require_metrics_access)):
    return Response(prometheus_text(), media_type="text/plain; version=0.0.4")
