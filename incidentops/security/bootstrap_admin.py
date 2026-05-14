from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

from sqlalchemy import func, select

from incidentops.config.settings import get_settings
from incidentops.db.models import ProjectMember, ProjectRole, User
from incidentops.db.session import _get_session_factory
from incidentops.security.audit import record_audit_event
from incidentops.security.passwords import hash_password


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap an IncidentOps admin user.")
    parser.add_argument("--email", default=os.getenv("BOOTSTRAP_ADMIN_EMAIL"))
    parser.add_argument("--password", default=os.getenv("BOOTSTRAP_ADMIN_PASSWORD"))
    parser.add_argument("--name", default=os.getenv("BOOTSTRAP_ADMIN_NAME", "IncidentOps Admin"))
    parser.add_argument("--project-id")
    parser.add_argument("--update-password", action="store_true")
    return parser


async def bootstrap_admin(
    *,
    email: str,
    password: str,
    name: str = "IncidentOps Admin",
    project_id: uuid.UUID | None = None,
    update_password: bool = False,
) -> str:
    settings = get_settings()
    if settings.is_production_like and password in {"incidentops", "password", "admin", "change-me"}:
        raise ValueError("Refusing weak/default bootstrap password in production-like environment")
    if settings.is_production_like and len(password) < 12:
        raise ValueError("Bootstrap password must be at least 12 characters in production-like environment")

    factory = _get_session_factory()
    async with factory() as db:
        result = await db.execute(select(User).where(func.lower(User.email) == email.lower()))
        user = result.scalar_one_or_none()
        if user:
            if update_password:
                user.password_hash = hash_password(password)
                user.password_scheme = "bcrypt"
                user.token_version = (user.token_version or 1) + 1
                action_status = "updated"
            else:
                action_status = "exists"
        else:
            user = User(
                email=email,
                name=name,
                password_hash=hash_password(password),
                password_scheme="bcrypt",
            )
            db.add(user)
            await db.flush()
            action_status = "created"

        if project_id is not None:
            member_result = await db.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == user.id,
                )
            )
            member = member_result.scalar_one_or_none()
            if member:
                member.role = ProjectRole.admin
            else:
                db.add(ProjectMember(project_id=project_id, user_id=user.id, role=ProjectRole.admin))

        await record_audit_event(
            db,
            action="bootstrap_admin_created" if action_status == "created" else "bootstrap_admin_checked",
            status=action_status,
            user=user,
            actor_email=email,
            resource_type="user",
            resource_id=user.id,
        )
        await db.commit()
        return action_status


async def _main_async() -> int:
    args = build_parser().parse_args()
    if not args.email or not args.password:
        print("email and password are required via args or BOOTSTRAP_ADMIN_EMAIL/PASSWORD", file=sys.stderr)
        return 2
    project_id = uuid.UUID(args.project_id) if args.project_id else None
    try:
        status = await bootstrap_admin(
            email=args.email,
            password=args.password,
            name=args.name,
            project_id=project_id,
            update_password=args.update_password,
        )
    except Exception as exc:
        print(f"bootstrap admin failed: {exc}", file=sys.stderr)
        return 1
    print(f"bootstrap admin {status}: {args.email}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main_async()))


if __name__ == "__main__":
    main()
