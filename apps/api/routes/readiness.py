from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import ensure_project_access, get_current_user, get_db, get_settings_dep
from incidentops.config.settings import Settings
from incidentops.db.models import ProjectRole
from incidentops.readiness.service import build_project_readiness
from incidentops.schemas.api import ReadinessResponse

router = APIRouter(prefix="/v1", tags=["Readiness"])


@router.get("/projects/{project_id}/readiness", response_model=ReadinessResponse)
async def get_project_readiness(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    settings: Settings = Depends(get_settings_dep),
):
    await ensure_project_access(db, project_id, user, settings, minimum_role=ProjectRole.viewer)
    return await build_project_readiness(db, project_id)
