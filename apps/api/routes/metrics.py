from __future__ import annotations

from fastapi import APIRouter

from incidentops.observability.metrics import summary
from incidentops.schemas.api import MetricsSummaryResponse

router = APIRouter(prefix="/v1/metrics", tags=["Metrics"])


@router.get("/summary", response_model=MetricsSummaryResponse)
async def metrics_summary():
    data = summary()
    return MetricsSummaryResponse(**data)
