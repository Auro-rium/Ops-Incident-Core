from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import check_rate_limit, get_db, get_settings_dep, require_user
from incidentops.config.settings import Settings
from incidentops.schemas.api import LoginRequest, LoginResponse
from incidentops.security.audit import record_login_event
from incidentops.security.auth import authenticate_user, create_access_token, ensure_local_seed_admin

router = APIRouter(prefix="/v1/auth", tags=["Auth"])


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
):
    client_host = request.client.host if request.client else "unknown"
    await check_rate_limit(
        db,
        settings,
        f"login:{client_host}:{body.email.lower()}",
        settings.user_request_limit,
        settings.rate_limit_window_seconds,
        action="login",
    )
    await ensure_local_seed_admin(db, settings)
    user = await authenticate_user(db, body.email, body.password, settings)
    if not user:
        await record_login_event(
            db,
            email=body.email,
            success=False,
            request=request,
            reason="invalid_credentials",
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


@router.get("/me")
async def me(user=Depends(require_user)):
    return {"id": str(user.id), "email": user.email, "name": user.name}
