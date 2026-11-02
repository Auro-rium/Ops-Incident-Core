"""
Async database engine and session factory.
"""

from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session

from incidentops.config.settings import get_settings

_engine = None
_session_factory = None


def _get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_size=20,
            max_overflow=10,
        )
    return _engine


def _get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            _get_engine(),
            expire_on_commit=False,
        )
    return _session_factory


_PENDING_WRITES_KEY = "incidentops_pending_writes"


@event.listens_for(Session, "after_flush")
def _mark_flushed(session, _flush_context) -> None:
    session.info[_PENDING_WRITES_KEY] = True


@event.listens_for(Session, "do_orm_execute")
def _mark_statement(orm_execute_state) -> None:
    # Core insert/update/delete (and raw text) bypass the unit of work, so
    # session.new/dirty/deleted never see them; track them explicitly.
    if not orm_execute_state.is_select:
        orm_execute_state.session.info[_PENDING_WRITES_KEY] = True


@event.listens_for(Session, "after_commit")
@event.listens_for(Session, "after_rollback")
def _clear_pending(session) -> None:
    session.info.pop(_PENDING_WRITES_KEY, None)


def has_pending_writes(session: Session | AsyncSession) -> bool:
    return bool(
        session.new
        or session.dirty
        or session.deleted
        or session.info.get(_PENDING_WRITES_KEY)
    )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async session; commit only uncommitted writes, rollback on error."""
    factory = _get_session_factory()
    async with factory() as session:
        try:
            yield session
            if has_pending_writes(session):
                await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_tables():
    """Create all tables — used for dev/testing. Use Alembic in prod."""
    from incidentops.db.base import Base
    import incidentops.db.models  # noqa: F401 — register models

    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_tables():
    from incidentops.db.base import Base
    import incidentops.db.models  # noqa: F401

    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
