from __future__ import annotations

import pytest

from apps.api.main import initialize_database_for_startup, should_run_create_all
from incidentops.config.settings import Settings
from incidentops.config.validation import production_settings_errors
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
    await initialize_database_for_startup(
        Settings(
            app_env="production",
            db_create_all=False,
            jwt_secret="x" * 40,
            allow_demo_project_bypass=False,
            allow_local_seed_admin=False,
            rate_limit_backend="redis",
            worker_mode="queue",
            job_queue_backend="redis",
            metrics_backend="prometheus",
            local_ingest_enabled=False,
            cors_allow_origins="https://incidentops.example.com",
            allow_wildcard_cors=False,
        )
    )


def test_alembic_head_resolves_to_security_migration():
    assert get_head_revision() == "0002_security_audit_events"


def test_required_tables_match_current_models():
    assert set(required_tables()) == set(Base.metadata.tables.keys())


def test_readiness_payload_ready_when_all_checks_pass():
    payload = build_readiness_payload(
        database_ok=True,
        pgvector_ok=True,
        existing_tables=set(required_tables()),
        current_revision="0002_security_audit_events",
        head_revision="0002_security_audit_events",
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
        head_revision="0002_security_audit_events",
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
        head_revision="0002_security_audit_events",
    )
    assert payload["ready"] is False
    assert payload["pgvector"] == "missing"
    assert payload["migration"] == "missing"


def test_production_validation_rejects_unsafe_security_defaults():
    errors = production_settings_errors(Settings(app_env="production"))
    assert any("JWT_SECRET" in error for error in errors)
    assert any("ALLOW_DEMO_PROJECT_BYPASS" in error for error in errors)
    assert any("ALLOW_LOCAL_SEED_ADMIN" in error for error in errors)
    assert any("RATE_LIMIT_BACKEND" in error for error in errors)
    assert any("WORKER_MODE" in error for error in errors)
    assert any("JOB_QUEUE_BACKEND" in error for error in errors)
    assert any("METRICS_BACKEND" in error for error in errors)
    assert any("LOCAL_INGEST_ENABLED" in error for error in errors)
