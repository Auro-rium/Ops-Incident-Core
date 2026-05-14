from __future__ import annotations

import pytest

from apps.api.main import initialize_database_for_startup, should_run_create_all
from incidentops.config.settings import Settings
from incidentops.db.base import Base
from incidentops.db.migrations import build_readiness_payload, get_head_revision, required_tables


def test_create_all_is_local_only_and_explicit():
    assert should_run_create_all(Settings(app_env="local", db_create_all=True)) is True
    assert should_run_create_all(Settings(app_env="development", db_create_all=True)) is True
    assert should_run_create_all(Settings(app_env="local", db_create_all=False)) is False
    assert should_run_create_all(Settings(app_env="staging", db_create_all=True)) is False
    assert should_run_create_all(Settings(app_env="production", db_create_all=True)) is False


@pytest.mark.asyncio
async def test_production_startup_does_not_call_create_all(monkeypatch):
    async def fail_create_tables():
        raise AssertionError("create_all should not run in production")

    monkeypatch.setattr("apps.api.main.create_tables", fail_create_tables)
    await initialize_database_for_startup(Settings(app_env="production", db_create_all=True))


def test_alembic_head_resolves_to_production_baseline():
    assert get_head_revision() == "0001_production_baseline"


def test_required_tables_match_current_models():
    assert set(required_tables()) == set(Base.metadata.tables.keys())


def test_readiness_payload_ready_when_all_checks_pass():
    payload = build_readiness_payload(
        database_ok=True,
        pgvector_ok=True,
        existing_tables=set(required_tables()),
        current_revision="0001_production_baseline",
        head_revision="0001_production_baseline",
    )
    assert payload["ready"] is True
    assert payload["required_tables"] == "ok"
    assert payload["migration"] == "ok"


def test_readiness_payload_reports_missing_tables_and_outdated_migration():
    existing = set(required_tables())
    existing.remove("chunks")
    payload = build_readiness_payload(
        database_ok=True,
        pgvector_ok=True,
        existing_tables=existing,
        current_revision="old_revision",
        head_revision="0001_production_baseline",
    )
    assert payload["ready"] is False
    assert payload["required_tables"] == ["chunks"]
    assert payload["migration"] == "outdated"


def test_readiness_payload_reports_missing_pgvector_and_missing_revision():
    payload = build_readiness_payload(
        database_ok=True,
        pgvector_ok=False,
        existing_tables=set(required_tables()),
        current_revision=None,
        head_revision="0001_production_baseline",
    )
    assert payload["ready"] is False
    assert payload["pgvector"] == "missing"
    assert payload["migration"] == "missing"
