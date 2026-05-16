from __future__ import annotations

from fastapi import APIRouter, Response

from incidentops.observability.metrics import prometheus_text, summary
from incidentops.schemas.api import MetricsSummaryResponse

router = APIRouter(tags=["Metrics"])


@router.get("/v1/metrics/summary", response_model=MetricsSummaryResponse)
async def metrics_summary():
    data = summary()
    return MetricsSummaryResponse(**data)


@router.get("/metrics")
async def prometheus_metrics():
    return Response(prometheus_text(), media_type="text/plain; version=0.0.4")
