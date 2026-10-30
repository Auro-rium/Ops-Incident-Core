from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from incidentops.config.settings import Settings
from incidentops.db.models import User
from incidentops.security.passwords import hash_password


def _request(peer: str | None, headers: dict[str, str] | None = None):
    return SimpleNamespace(
        client=SimpleNamespace(host=peer) if peer else None,
        headers={key.lower(): value for key, value in (headers or {}).items()},
    )


# ── client_ip ────────────────────────────────


def test_client_ip_ignores_forwarded_for_from_untrusted_peer():
    from incidentops.security.client_ip import client_ip

    settings = Settings(trusted_proxies="")
    request = _request("203.0.113.9", {"X-Forwarded-For": "1.2.3.4"})
    assert client_ip(request, settings) == "203.0.113.9"


def test_client_ip_honours_forwarded_for_from_trusted_proxy():
    from incidentops.security.client_ip import client_ip

    settings = Settings(trusted_proxies="10.0.0.0/8, 100.64.0.0/10")
    request = _request("10.1.2.3", {"X-Forwarded-For": "1.2.3.4, 198.51.100.7, 100.64.0.5"})
    # Right-most untrusted hop is the real client; earlier entries are client-controlled.
    assert client_ip(request, settings) == "198.51.100.7"


def test_client_ip_handles_garbage_forwarded_for():
    from incidentops.security.client_ip import client_ip

    settings = Settings(trusted_proxies="10.0.0.0/8")
    request = _request("10.1.2.3", {"X-Forwarded-For": "not-an-ip"})
    assert client_ip(request, settings) == "10.1.2.3"
    assert client_ip(_request(None), settings) is None


def test_audit_ip_address_uses_trusted_proxy_setting(monkeypatch):
    from incidentops.security import audit

    monkeypatch.setattr(audit, "get_settings", lambda: Settings(trusted_proxies=""))
    request = _request("203.0.113.9", {"X-Forwarded-For": "1.2.3.4"})
    assert audit._ip_address(request) == "203.0.113.9"


# ── authenticate_user ────────────────────────


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _CapturingSession:
    def __init__(self, user: User | None):
        self.user = user
        self.statements = []
        self.added = []
        self.commits = 0

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(statement)
        return _FakeResult(self.user)

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None


def _user(password: str = "correct-password", **overrides) -> User:
    values = dict(
        id=uuid.uuid4(),
        email="admin@example.com",
        name="Admin",
        password_hash=hash_password(password),
        password_scheme="bcrypt",
        token_version=1,
        failed_login_count=0,
        is_active=True,
        disabled_at=None,
        locked_until=None,
    )
    values.update(overrides)
    return User(**values)


async def test_authenticate_user_lookup_is_case_insensitive():
    from incidentops.security.auth import authenticate_user

    session = _CapturingSession(_user())
    user, reason = await authenticate_user(session, "Admin@Example.COM ", "correct-password", Settings())
    assert user is not None and reason is None
    compiled = session.statements[0].compile(compile_kwargs={"literal_binds": True})
    sql = str(compiled).lower()
    assert "lower(users.email)" in sql
    assert "'admin@example.com'" in sql


async def test_authenticate_user_locks_after_max_failures():
    from incidentops.security.auth import authenticate_user

    settings = Settings(login_max_failed_attempts=3, login_lockout_minutes=15)
    target = _user(failed_login_count=2)
    session = _CapturingSession(target)
    user, reason = await authenticate_user(session, "admin@example.com", "wrong", settings)
    assert user is None
    assert reason == "account_locked"
    assert target.locked_until is not None
    remaining = target.locked_until - datetime.now(timezone.utc)
    assert timedelta(minutes=14) < remaining <= timedelta(minutes=15)


async def test_authenticate_user_rejects_locked_account_even_with_correct_password():
    from incidentops.security.auth import authenticate_user

    target = _user(locked_until=datetime.now(timezone.utc) + timedelta(minutes=5))
    session = _CapturingSession(target)
    user, reason = await authenticate_user(session, "admin@example.com", "correct-password", Settings())
    assert user is None
    assert reason == "account_locked"


async def test_authenticate_user_success_resets_counter_and_expired_lock():
    from incidentops.security.auth import authenticate_user

    target = _user(failed_login_count=4, locked_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    session = _CapturingSession(target)
    user, reason = await authenticate_user(session, "admin@example.com", "correct-password", Settings())
    assert user is target and reason is None
    assert target.failed_login_count == 0
    assert target.locked_until is None


async def test_authenticate_user_reports_password_reset_for_legacy_hash_in_prod():
    import hashlib

    from incidentops.security.auth import authenticate_user

    legacy = hashlib.sha256(b"correct-password").hexdigest()
    target = _user(password_hash=legacy, password_scheme="sha256")
    session = _CapturingSession(target)
    user, reason = await authenticate_user(
        session, "admin@example.com", "correct-password", Settings(app_env="production")
    )
    assert user is None
    assert reason == "password_reset_required"


# ── bootstrap admin ──────────────────────────


async def test_bootstrap_admin_stores_lowercase_email(monkeypatch):
    from incidentops.security import bootstrap_admin as module

    session = _CapturingSession(None)

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return session

        async def __aexit__(self, *_exc):
            return False

    async def _noop_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(module, "_get_session_factory", lambda: _Factory())
    monkeypatch.setattr(module, "record_audit_event", _noop_audit)
    status = await module.bootstrap_admin(email="Ops.Admin@Example.com", password="a-strong-password-123")
    assert status == "created"
    created = [item for item in session.added if isinstance(item, User)]
    assert created[0].email == "ops.admin@example.com"


# ── login / logout routes ────────────────────


def _auth_app(monkeypatch, session, settings: Settings, user: User | None = None):
    from apps.api import deps
    from apps.api.routes import auth as auth_routes

    app = FastAPI()
    app.include_router(auth_routes.router)

    async def _db():
        yield session

    app.dependency_overrides[deps.get_db] = _db
    app.dependency_overrides[deps.get_settings_dep] = lambda: settings
    if user is not None:
        app.dependency_overrides[deps.get_current_user] = lambda: user
    return app


def test_login_route_does_not_seed_admin_per_request():
    from apps.api.routes import auth as auth_routes

    assert not hasattr(auth_routes, "ensure_local_seed_admin")


def test_login_limits_attempts_per_email_across_ips(monkeypatch):
    from apps.api.routes import auth as auth_routes
    from incidentops.security import rate_limit

    monkeypatch.setattr(rate_limit, "_memory_limiter", rate_limit.InMemoryRateLimiter())
    events: list[dict] = []

    async def _auth(*_args, **_kwargs):
        return None, "invalid_credentials"

    async def _login_event(*_args, **kwargs):
        events.append(kwargs)

    async def _rate_event(*_args, **_kwargs):
        return None

    monkeypatch.setattr(auth_routes, "authenticate_user", _auth)
    monkeypatch.setattr(auth_routes, "record_login_event", _login_event)
    monkeypatch.setattr("apps.api.deps.record_rate_limit_exceeded", _rate_event)
    settings = Settings(user_request_limit=100, login_email_request_limit=2, trusted_proxies="")
    app = _auth_app(monkeypatch, _CapturingSession(None), settings)
    client = TestClient(app)
    codes = []
    for index in range(3):
        # Different (spoofed) forwarded IPs must not reset the per-email budget.
        response = client.post(
            "/v1/auth/login",
            json={"email": "Victim@Example.com", "password": "guess"},
            headers={"X-Forwarded-For": f"198.51.100.{index}"},
        )
        codes.append(response.status_code)
    assert codes == [401, 401, 429]
    assert events[0]["reason"] == "invalid_credentials"


def test_login_failure_audit_records_specific_reason_but_generic_response(monkeypatch):
    from apps.api.routes import auth as auth_routes
    from incidentops.security import rate_limit

    monkeypatch.setattr(rate_limit, "_memory_limiter", rate_limit.InMemoryRateLimiter())
    events: list[dict] = []

    async def _auth(*_args, **_kwargs):
        return None, "password_reset_required"

    async def _login_event(*_args, **kwargs):
        events.append(kwargs)

    monkeypatch.setattr(auth_routes, "authenticate_user", _auth)
    monkeypatch.setattr(auth_routes, "record_login_event", _login_event)
    app = _auth_app(monkeypatch, _CapturingSession(None), Settings())
    response = TestClient(app).post("/v1/auth/login", json={"email": "a@example.com", "password": "pw1"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid credentials"
    assert events[0]["reason"] == "password_reset_required"


def test_logout_all_bumps_token_version(monkeypatch):
    from apps.api.routes import auth as auth_routes

    async def _audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(auth_routes, "record_audit_event", _audit)
    target = _user(token_version=3)
    session = _CapturingSession(target)
    app = _auth_app(monkeypatch, session, Settings(), user=target)
    response = TestClient(app).post("/v1/auth/logout-all")
    assert response.status_code == 200
    assert target.token_version == 4
    assert session.commits == 1


def test_logout_all_requires_auth(monkeypatch):
    from apps.api import deps

    app = _auth_app(monkeypatch, _CapturingSession(None), Settings())
    app.dependency_overrides[deps.get_current_user] = lambda: None
    response = TestClient(app).post("/v1/auth/logout-all")
    assert response.status_code == 401


# ── startup seed admin ───────────────────────


async def test_startup_seeds_local_admin_once_and_tolerates_errors(monkeypatch):
    from apps.api import main

    calls = []

    async def _seed(db, settings):
        calls.append(settings)
        raise RuntimeError("relation users does not exist")

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _CapturingSession(None)

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(main, "ensure_local_seed_admin", _seed)
    monkeypatch.setattr(main, "_get_session_factory", lambda: _Factory())
    await main.seed_local_admin_on_startup(Settings(app_env="local", allow_local_seed_admin=True))
    assert len(calls) == 1
    calls.clear()
    await main.seed_local_admin_on_startup(Settings(app_env="production", allow_local_seed_admin=False))
    assert calls == []
