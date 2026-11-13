from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from apps.api import deps
from apps.api.main import resolve_cors_origins
from apps.api.routes import investigate as investigate_route
from apps.api.routes import metrics as metrics_route
from incidentops.config.settings import Settings
from incidentops.mcp import server as mcp_server
from incidentops.schemas.api import InvestigationRequest


# ── "disabled" Key Vault placeholders ────────
# Key Vault cannot store an empty secret, so Bicep writes "disabled" for unset
# ones. That value must never work as a credential.


@pytest.mark.parametrize("value", ["disabled", " Disabled ", ""])
def test_disabled_placeholder_never_becomes_a_usable_token(value):
    settings = Settings(_env_file=None, metrics_token=value, mcp_inbound_token=value)
    assert settings.metrics_token == ""
    assert settings.mcp_inbound_token == ""


def test_real_tokens_survive_placeholder_normalization():
    settings = Settings(_env_file=None, metrics_token="scrape-me", mcp_inbound_token="inbound")
    assert settings.metrics_token == "scrape-me"
    assert settings.mcp_inbound_token == "inbound"


def test_metrics_rejects_disabled_placeholder_as_a_bearer_token():
    settings = Settings(_env_file=None, metrics_token="disabled")
    assert metrics_route._has_metrics_token("Bearer disabled", settings) is False


def test_mcp_http_app_fails_closed_on_disabled_placeholder():
    placeholder = Settings(_env_file=None, mcp_inbound_token="disabled").mcp_inbound_token
    with pytest.raises(RuntimeError):
        mcp_server.BearerTokenMiddleware(lambda *args: None, placeholder)


# ── CORS ─────────────────────────────────────


def test_unset_cors_env_allows_no_origins(monkeypatch):
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert resolve_cors_origins(Settings(_env_file=None)) == []


# ── investigate route ────────────────────────


class _StopRoute(Exception):
    pass


async def test_investigate_rate_limits_anonymous_callers_by_ip(monkeypatch):
    calls = []

    async def _check(db, settings, key, limit, window_seconds=None, **kwargs):
        calls.append(key)

    async def _access(*args, **kwargs):
        raise _StopRoute

    monkeypatch.setattr(investigate_route, "check_rate_limit", _check)
    monkeypatch.setattr(investigate_route, "ensure_project_access", _access)
    monkeypatch.setattr(deps, "client_ip", lambda request, settings: "203.0.113.9")

    body = InvestigationRequest(project_id=uuid.uuid4(), query="why", top_k=3)
    with pytest.raises(_StopRoute):
        await investigate_route.investigate(
            body, SimpleNamespace(), db=object(), settings=Settings(_env_file=None), user=None
        )
    assert calls == ["investigate:ip:203.0.113.9"]


async def test_investigate_audit_names_the_resource_it_actually_identifies(monkeypatch):
    audit = {}

    async def _noop(*args, **kwargs):
        return None

    async def _run(*args, **kwargs):
        return SimpleNamespace(task_type="incident"), 1

    async def _audit(db, **kwargs):
        audit.update(kwargs)
        raise _StopRoute

    monkeypatch.setattr(investigate_route, "check_rate_limit", _noop)
    monkeypatch.setattr(investigate_route, "ensure_project_access", _noop)
    monkeypatch.setattr(investigate_route, "run_investigation", _run)
    monkeypatch.setattr(investigate_route, "record_audit_event", _audit)

    project_id = uuid.uuid4()
    body = InvestigationRequest(project_id=project_id, query="why", top_k=3)
    with pytest.raises(_StopRoute):
        await investigate_route.investigate(
            body, SimpleNamespace(), db=object(), settings=Settings(_env_file=None), user=None
        )
    assert audit["resource_type"] == "project"
    assert audit["resource_id"] == project_id
