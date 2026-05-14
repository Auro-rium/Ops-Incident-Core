from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.db.base import Base
from incidentops.db.session import _get_session_factory

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def required_tables() -> list[str]:
    return sorted(Base.metadata.tables.keys())


def get_alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


def get_head_revision() -> str | None:
    script = ScriptDirectory.from_config(get_alembic_config())
    return script.get_current_head()


def build_readiness_payload(
    *,
    database_ok: bool,
    pgvector_ok: bool,
    existing_tables: set[str],
    current_revision: str | None,
    head_revision: str | None,
    database_error: str | None = None,
) -> dict[str, Any]:
    missing_tables = [table for table in required_tables() if table not in existing_tables]
    if not database_ok:
        migration_status = "unknown"
    elif current_revision is None:
        migration_status = "missing"
    elif head_revision is None:
        migration_status = "unknown"
    elif current_revision == head_revision:
        migration_status = "ok"
    else:
        migration_status = "outdated"

    payload: dict[str, Any] = {
        "ready": database_ok and pgvector_ok and not missing_tables and migration_status == "ok",
        "database": "ok" if database_ok else "error",
        "pgvector": "ok" if pgvector_ok else "missing",
        "required_tables": "ok" if not missing_tables else missing_tables,
        "migration": migration_status,
        "current_revision": current_revision,
        "head_revision": head_revision,
    }
    if database_error:
        payload["database_error"] = database_error
    return payload


async def check_database_ready(db: AsyncSession | None = None) -> dict[str, Any]:
    if db is None:
        factory = _get_session_factory()
        async with factory() as session:
            return await _check_database_ready_with_session(session)
    return await _check_database_ready_with_session(db)


async def _check_database_ready_with_session(db: AsyncSession) -> dict[str, Any]:
    head_revision = get_head_revision()
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        await db.rollback()
        return build_readiness_payload(
            database_ok=False,
            pgvector_ok=False,
            existing_tables=set(),
            current_revision=None,
            head_revision=head_revision,
            database_error=f"{exc.__class__.__name__}: {exc}",
        )

    pgvector_ok = bool(
        (
            await db.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
            )
        ).scalar()
    )
    table_rows = await db.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
    )
    existing_tables = {row[0] for row in table_rows.all()}
    current_revision = None
    if "alembic_version" in existing_tables:
        current_revision = (
            await db.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        ).scalar_one_or_none()

    return build_readiness_payload(
        database_ok=True,
        pgvector_ok=pgvector_ok,
        existing_tables=existing_tables,
        current_revision=current_revision,
        head_revision=head_revision,
    )
