from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from incidentops.db.base import Base
import incidentops.db.models  # noqa: F401 - register SQLAlchemy models for metadata checks
from incidentops.db.session import _get_session_factory

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def required_tables() -> list[str]:
    return sorted(Base.metadata.tables.keys())


def required_columns() -> dict[str, list[str]]:
    return {
        table_name: sorted(column.name for column in table.columns)
        for table_name, table in Base.metadata.tables.items()
    }


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
    existing_columns: Mapping[str, set[str]] | None = None,
    current_revision: str | None,
    head_revision: str | None,
    database_error: str | None = None,
) -> dict[str, Any]:
    missing_tables = [table for table in required_tables() if table not in existing_tables]
    missing_columns: dict[str, list[str]] = {}
    if existing_columns is not None:
        for table_name, columns in required_columns().items():
            if table_name not in existing_tables:
                continue
            table_columns = existing_columns.get(table_name, set())
            missing = [column for column in columns if column not in table_columns]
            if missing:
                missing_columns[table_name] = missing

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
        "ready": (
            database_ok
            and pgvector_ok
            and not missing_tables
            and not missing_columns
            and migration_status == "ok"
        ),
        "database": "ok" if database_ok else "error",
        "pgvector": "ok" if pgvector_ok else "missing",
        "required_tables": "ok" if not missing_tables else missing_tables,
        "required_columns": "ok" if not missing_columns else missing_columns,
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
    column_rows = await db.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public'"
        )
    )
    existing_columns: dict[str, set[str]] = {}
    for table_name, column_name in column_rows.all():
        existing_columns.setdefault(table_name, set()).add(column_name)
    current_revision = None
    if "alembic_version" in existing_tables:
        current_revision = (
            await db.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        ).scalar_one_or_none()

    return build_readiness_payload(
        database_ok=True,
        pgvector_ok=pgvector_ok,
        existing_tables=existing_tables,
        existing_columns=existing_columns,
        current_revision=current_revision,
        head_revision=head_revision,
    )
