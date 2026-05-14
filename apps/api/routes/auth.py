from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_db, get_settings_dep, require_user
from incidentops.config.settings import Settings
from incidentops.schemas.api import LoginRequest, LoginResponse
from incidentops.security.auth import authenticate_user, encode_token, ensure_seed_admin

router = APIRouter(prefix="/v1/auth", tags=["Auth"])


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
):
    await ensure_seed_admin(db)
    user = await authenticate_user(db, body.email, body.password)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token = encode_token({"sub": str(user.id), "email": user.email}, settings.auth_secret, settings.auth_token_ttl_seconds)
    return LoginResponse(
        access_token=token,
        user={"id": str(user.id), "email": user.email, "name": user.name},
    )


@router.get("/me")
async def me(user=Depends(require_user)):
    return {"id": str(user.id), "email": user.email, "name": user.name}
