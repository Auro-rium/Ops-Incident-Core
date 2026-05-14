from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from incidentops.db.migrations import check_database_ready

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/ready")
async def ready():
    payload = await check_database_ready()
    ready_status = payload.pop("ready")
    payload = {"status": "ready" if ready_status else "not_ready", **payload}
    return JSONResponse(payload, status_code=200 if ready_status else 503)
