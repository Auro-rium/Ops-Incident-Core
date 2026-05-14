from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.db.models import ProjectMember, ProjectRole, User


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    return hash_password(password) == password_hash


def encode_token(payload: dict, secret: str, expires_in_seconds: int) -> str:
    body = payload | {"exp": int(time.time()) + expires_in_seconds}
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    data = urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
    sig = hmac.new(secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{data}.{sig}"


def decode_token(token: str, secret: str) -> dict:
    try:
        data, sig = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
    expected = hmac.new(secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid token signature")
    payload = json.loads(urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8"))
    if payload.get("exp", 0) < int(time.time()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    return payload


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user and verify_password(password, user.password_hash):
        return user
    return None


async def ensure_seed_admin(db: AsyncSession) -> None:
    result = await db.execute(select(User).where(User.email == "admin@incidentops.local"))
    user = result.scalar_one_or_none()
    if user:
        return
    admin = User(
        email="admin@incidentops.local",
        name="IncidentOps Admin",
        password_hash=hash_password("incidentops"),
    )
    db.add(admin)
    await db.flush()
