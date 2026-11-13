from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
import jwt
from jwt import ExpiredSignatureError, InvalidTokenError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.config.settings import Settings
from incidentops.db.models import User
from incidentops.security.passwords import (
    hash_password,
    is_legacy_sha256_hash,
    needs_rehash,
    verify_password,
)

_LOCAL_SEED_ADMIN_EMAIL = "admin@incidentops.local"


def create_access_token(user: User, settings: Settings) -> tuple[str, int]:
    now = datetime.now(timezone.utc)
    expires_delta = timedelta(minutes=settings.access_token_expire_minutes)
    expires_at = now + expires_delta
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "token_type": "access",
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "token_version": getattr(user, "token_version", 1),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, int(expires_delta.total_seconds())


def decode_token(token: str, settings: Settings) -> dict:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["sub", "exp", "iat", "iss", "aud", "token_type"]},
        )
    except ExpiredSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token expired") from exc
    except InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
    if payload.get("token_type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
    return payload


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def authenticate_user(
    db: AsyncSession, email: str, password: str, settings: Settings
) -> tuple[User | None, str | None]:
    """Return ``(user, None)`` on success or ``(None, reason)`` on failure.

    The reason is for audit logs only; callers must answer with a generic error.
    """
    result = await db.execute(select(User).where(func.lower(User.email) == normalize_email(email)))
    user = result.scalar_one_or_none()
    if not user or not user.is_active or user.disabled_at is not None:
        return None, "invalid_credentials"

    now = datetime.now(timezone.utc)
    if user.locked_until is not None and _as_aware(user.locked_until) > now:
        return None, "account_locked"

    allow_legacy = not settings.is_production_like
    if not allow_legacy and is_legacy_sha256_hash(user.password_hash):
        # Legacy SHA-256 hashes are never accepted in production-like environments;
        # an admin must reset the password (bootstrap_admin --update-password).
        return None, "password_reset_required"

    if not verify_password(password, user.password_hash, allow_legacy_sha256=allow_legacy):
        user.failed_login_count = (user.failed_login_count or 0) + 1
        if user.failed_login_count >= max(1, settings.login_max_failed_attempts):
            user.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
            user.failed_login_count = 0
            return None, "account_locked"
        return None, "invalid_credentials"

    if allow_legacy and needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        user.password_scheme = "bcrypt"

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    return user, None


async def ensure_local_seed_admin(db: AsyncSession, settings: Settings) -> None:
    if settings.is_production_like or not settings.allow_local_seed_admin:
        return
    result = await db.execute(select(User).where(func.lower(User.email) == _LOCAL_SEED_ADMIN_EMAIL))
    user = result.scalar_one_or_none()
    if user:
        return
    admin = User(
        email=_LOCAL_SEED_ADMIN_EMAIL,
        name="IncidentOps Admin",
        password_hash=hash_password("incidentops"),
        password_scheme="bcrypt",
    )
    db.add(admin)
    await db.flush()
