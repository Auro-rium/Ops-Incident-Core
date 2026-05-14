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
from incidentops.security.audit import record_rate_limit_exceeded
from incidentops.security.rate_limit import get_rate_limiter
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
    payload = decode_token(token, settings)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
    try:
        parsed_user_id = uuid.UUID(user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload") from exc
    result = await db.execute(select(User).where(User.id == parsed_user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if not user.is_active or user.disabled_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="User disabled")
    if int(payload.get("token_version", 1)) != int(user.token_version or 1):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token revoked")
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
    if _project_bypass_allowed(project, settings):
        return
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    await require_project_role(db, project_id, user.id, minimum_role)


def _project_bypass_allowed(project: Project, settings: Settings) -> bool:
    if settings.is_production_like:
        return False
    if settings.demo_mode_public:
        return True
    return bool(project.demo_mode and settings.allow_demo_project_bypass)


def enforce_query_limits(query: str, top_k: int, settings: Settings) -> None:
    if len(query) > settings.max_query_length:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Query exceeds MAX_QUERY_LENGTH")
    if top_k > settings.max_top_k or top_k > settings.max_retrieved_chunks:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="top_k exceeds configured limit")


def enforce_json_size(payload: object, max_bytes: int, label: str) -> None:
    import json

    size = len(json.dumps(payload or {}, default=str).encode("utf-8"))
    if size > max_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=f"{label} exceeds configured size limit")


async def check_rate_limit(
    db: AsyncSession,
    settings: Settings,
    key: str,
    limit: int,
    window_seconds: int | None = None,
    *,
    user: User | None = None,
    project_id: uuid.UUID | None = None,
    action: str = "request",
) -> None:
    if not settings.rate_limit_enabled:
        return
    try:
        get_rate_limiter(settings).check(key, limit, window_seconds or settings.rate_limit_window_seconds)
    except HTTPException:
        await record_rate_limit_exceeded(
            db,
            user=user,
            project_id=project_id,
            action=action,
            key=key,
            limit=limit,
        )
        raise
