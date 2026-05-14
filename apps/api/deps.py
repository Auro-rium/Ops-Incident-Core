"""
FastAPI dependencies.
"""

from __future__ import annotations

import uuid
from typing import AsyncGenerator

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.config.settings import Settings, get_settings
from incidentops.db.models import ProjectRole, User
from incidentops.db.models import Project
from incidentops.db.session import get_db as _get_db
from incidentops.security.auth import decode_token
from incidentops.security.rbac import require_project_role


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async for session in _get_db():
        yield session


def get_settings_dep() -> Settings:
    return get_settings()


async def get_current_user(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> User | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid authorization header")
    payload = decode_token(token, settings.auth_secret)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def require_user(user: User | None = Depends(get_current_user)) -> User:
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user


async def require_viewer(
    project_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    await require_project_role(db, project_id, user.id, ProjectRole.viewer)
    return user


async def require_investigator(
    project_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    await require_project_role(db, project_id, user.id, ProjectRole.investigator)
    return user


async def require_approver(
    project_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    await require_project_role(db, project_id, user.id, ProjectRole.approver)
    return user


async def ensure_project_access(
    db: AsyncSession,
    project_id: uuid.UUID,
    user: User | None,
    settings: Settings,
    minimum_role: ProjectRole = ProjectRole.viewer,
) -> None:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Project not found")
    if project.demo_mode or settings.demo_mode_public:
        return
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    await require_project_role(db, project_id, user.id, minimum_role)
