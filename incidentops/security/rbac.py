from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.db.models import ProjectMember, ProjectRole, User
from incidentops.security.permission_policy import role_allows


async def get_project_role(db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID) -> ProjectRole | None:
    result = await db.execute(
        select(ProjectMember.role).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def require_project_role(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    minimum_role: ProjectRole,
) -> ProjectRole:
    actual = await get_project_role(db, project_id, user_id)
    if actual is None or not role_allows(actual, minimum_role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Insufficient project permissions")
    return actual
