from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import check_rate_limit, get_db, get_settings_dep, require_user
from incidentops.config.settings import Settings
from incidentops.db.models import User
from incidentops.schemas.api import LoginRequest, LoginResponse
from incidentops.security.audit import record_audit_event, record_login_event
from incidentops.security.auth import authenticate_user, create_access_token, normalize_email
from incidentops.security.client_ip import client_ip

logger = logging.getLogger("incidentops.api.auth")

router = APIRouter(prefix="/v1/auth", tags=["Auth"])


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
):
    client_host = client_ip(request, settings) or "unknown"
    email = normalize_email(body.email)
    await check_rate_limit(
        db,
        settings,
        f"login:{client_host}:{email}",
        settings.user_request_limit,
        settings.rate_limit_window_seconds,
        action="login",
    )
    # Per-email budget across all IPs, so rotating source addresses cannot
    # brute-force a single account.
    await check_rate_limit(
        db,
        settings,
        f"login-email:{email}",
        settings.login_email_request_limit,
        settings.rate_limit_window_seconds,
        action="login",
    )
    user, reason = await authenticate_user(db, email, body.password, settings)
    if not user:
        reason = reason or "invalid_credentials"
        if reason == "password_reset_required":
            logger.warning(
                "Login rejected for %s: legacy SHA-256 password hash; an admin must reset the password",
                email,
            )
        await record_login_event(
            db,
            email=email,
            success=False,
            request=request,
            reason=reason,
            commit=True,
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token, expires_in = create_access_token(user, settings)
    await record_login_event(db, email=user.email, success=True, request=request, user=user, commit=True)
    return LoginResponse(
        access_token=token,
        expires_in=expires_in,
        user={"id": str(user.id), "email": user.email, "name": user.name},
    )


@router.post("/logout-all")
async def logout_all(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
):
    """Revoke every outstanding access token for the caller."""
    user.token_version = (user.token_version or 1) + 1
    await record_audit_event(
        db,
        action="logout_all",
        status="success",
        user=user,
        resource_type="user",
        resource_id=user.id,
        request=request,
    )
    await db.commit()
    return {"revoked": True, "token_version": user.token_version}


@router.get("/me")
async def me(user=Depends(require_user)):
    return {"id": str(user.id), "email": user.email, "name": user.name}
