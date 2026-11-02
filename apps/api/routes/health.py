from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from incidentops.db.migrations import check_database_ready

logger = logging.getLogger("incidentops.api.health")

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/ready")
async def ready():
    payload = await check_database_ready()
    ready_status = payload.get("ready") is True
    if not ready_status:
        logger.warning("Readiness check failed: %s", payload)
    # /ready is unauthenticated: expose booleans only, details stay in the logs.
    body = {
        "status": "ready" if ready_status else "not_ready",
        "database": payload.get("database") == "ok",
        "vector_store": payload.get("vector_store") in {"ok", "skipped"},
        "schema": payload.get("required_tables") == "ok" and payload.get("required_columns") == "ok",
        "migrations": payload.get("migration") == "ok",
    }
    return JSONResponse(body, status_code=200 if ready_status else 503)
