from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.db.models import AuditEvent, User
from incidentops.security.secret_redaction import redact_secrets

logger = logging.getLogger("incidentops.security.audit")

_SENSITIVE_KEY_TOKENS = {
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "client_secret",
    "access_key",
    "refresh_token",
    "bearer",
    "credential",
}


async def record_audit_event(
    db: AsyncSession,
    *,
    action: str,
    status: str,
    project_id: uuid.UUID | None = None,
    user: User | None = None,
    user_id: uuid.UUID | None = None,
    actor_email: str | None = None,
    resource_type: str | None = None,
    resource_id: str | uuid.UUID | None = None,
    request: Request | None = None,
    metadata: dict[str, Any] | None = None,
    commit: bool = False,
) -> None:
    try:
        event = AuditEvent(
            project_id=project_id,
            user_id=user.id if user else user_id,
            actor_email=actor_email or (user.email if user else None),
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            status=status,
            ip_address=_ip_address(request),
            user_agent=request.headers.get("user-agent") if request else None,
            metadata_json=sanitize_audit_metadata(metadata or {}),
        )
        db.add(event)
        await db.flush()
        if commit:
            await db.commit()
    except Exception:
        logger.exception("Failed recording audit event action=%s status=%s", action, status)
        if commit:
            await db.rollback()


async def record_login_event(
    db: AsyncSession,
    *,
    email: str,
    success: bool,
    request: Request | None = None,
    user: User | None = None,
    reason: str | None = None,
    commit: bool = False,
) -> None:
    await record_audit_event(
        db,
        action="login_success" if success else "login_failed",
        status="success" if success else "failure",
        user=user,
        actor_email=email,
        request=request,
        metadata={"reason": reason} if reason else {},
        commit=commit,
    )


async def record_permission_denied(
    db: AsyncSession,
    *,
    project_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    action: str = "permission_denied",
    resource_type: str | None = "project",
    resource_id: str | uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
) -> None:
    await record_audit_event(
        db,
        action=action,
        status="denied",
        project_id=project_id,
        user_id=user_id,
        resource_type=resource_type,
        resource_id=resource_id or project_id,
        metadata=metadata,
        commit=commit,
    )


async def record_rate_limit_exceeded(
    db: AsyncSession,
    *,
    user: User | None,
    project_id: uuid.UUID | None,
    action: str,
    key: str,
    limit: int,
) -> None:
    await record_audit_event(
        db,
        action="rate_limit_exceeded",
        status="blocked",
        project_id=project_id,
        user=user,
        resource_type="rate_limit",
        resource_id=action,
        metadata={"key": key, "limit": limit},
        commit=True,
    )


def sanitize_audit_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if any(token in normalized for token in _SENSITIVE_KEY_TOKENS):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = sanitize_audit_metadata(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_audit_metadata(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value


def _ip_address(request: Request | None) -> str | None:
    if not request or not request.client:
        return None
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.client.host
