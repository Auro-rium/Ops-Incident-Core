from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user, get_db, get_settings_dep
from incidentops.config.settings import Settings
from incidentops.observability.metrics import prometheus_text, summary
from incidentops.schemas.api import MetricsSummaryResponse

router = APIRouter(tags=["Metrics"])


def _has_metrics_token(authorization: str | None, settings: Settings) -> bool:
    expected = settings.metrics_token.strip()
    if not expected or not authorization:
        return False
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    return hmac.compare_digest(token.strip().encode("utf-8"), expected.encode("utf-8"))


async def require_metrics_access(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    db: AsyncSession = Depends(get_db),
) -> None:
    if settings.metrics_public:
        return
    authorization = request.headers.get("authorization")
    # A Prometheus scrape token is checked before JWT decoding, which would
    # otherwise reject the opaque token as an invalid JWT.
    if _has_metrics_token(authorization, settings):
        return
    user = await get_current_user(authorization=authorization, db=db, settings=settings)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


@router.get("/v1/metrics/summary", response_model=MetricsSummaryResponse)
async def metrics_summary(_: None = Depends(require_metrics_access)):
    data = summary()
    return MetricsSummaryResponse(**data)


@router.get("/metrics")
async def prometheus_metrics(_: None = Depends(require_metrics_access)):
    return Response(prometheus_text(), media_type="text/plain; version=0.0.4")
