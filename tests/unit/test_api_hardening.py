from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from incidentops.config.settings import Settings


# ── rate limiting ────────────────────────────


class _FakeAsyncPipeline:
    def __init__(self, store: dict[str, dict[str, float]]):
        self._store = store
        self._ops: list[tuple] = []

    def zremrangebyscore(self, key, minimum, maximum):
        self._ops.append(("zrem", key, minimum, maximum))
        return self

    def zcard(self, key):
        self._ops.append(("zcard", key))
        return self

    def zadd(self, key, mapping):
        self._ops.append(("zadd", key, mapping))
        return self

    def expire(self, key, seconds):
        self._ops.append(("expire", key, seconds))
        return self

    async def execute(self):
        results = []
        for op in self._ops:
            bucket = self._store.setdefault(op[1], {})
            if op[0] == "zrem":
                for member, score in list(bucket.items()):
                    if op[2] <= score <= op[3]:
                        del bucket[member]
                results.append(0)
            elif op[0] == "zcard":
                results.append(len(bucket))
            elif op[0] == "zadd":
                bucket.update(op[2])
                results.append(len(op[2]))
            else:
                results.append(True)
        self._ops = []
        return results


class _FakeAsyncRedis:
    def __init__(self):
        self.store: dict[str, dict[str, float]] = {}

    def pipeline(self, *_args, **_kwargs):
        return _FakeAsyncPipeline(self.store)


def _redis_limiter():
    from incidentops.security.rate_limit import RedisRateLimiter

    limiter = RedisRateLimiter.__new__(RedisRateLimiter)
    limiter._client = _FakeAsyncRedis()
    return limiter


async def test_redis_rate_limiter_uses_unique_members_for_same_timestamp(monkeypatch):
    monkeypatch.setattr("incidentops.security.rate_limit.time.time", lambda: 1000.0)
    limiter = _redis_limiter()
    await limiter.check("k", 5, 60)
    await limiter.check("k", 5, 60)
    assert len(limiter._client.store["incidentops:rate:k"]) == 2


async def test_redis_rate_limiter_does_not_count_rejected_requests(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr("incidentops.security.rate_limit.time.time", lambda: clock["now"])
    limiter = _redis_limiter()
    await limiter.check("k", 2, 60)
    clock["now"] += 1
    await limiter.check("k", 2, 60)
    for _ in range(5):
        clock["now"] += 1
        with pytest.raises(HTTPException):
            await limiter.check("k", 2, 60)
    assert len(limiter._client.store["incidentops:rate:k"]) == 2
    # Once the accepted requests age out, the caller is allowed again.
    clock["now"] = 1000.0 + 61.5
    await limiter.check("k", 2, 60)


def test_redis_rate_limiter_uses_asyncio_client(monkeypatch):
    import redis.asyncio

    from incidentops.security.rate_limit import RedisRateLimiter

    limiter = RedisRateLimiter("redis://localhost:6379/0")
    assert isinstance(limiter._client, redis.asyncio.Redis)


async def test_check_rate_limit_awaits_limiter(monkeypatch):
    from apps.api import deps
    from incidentops.security import rate_limit

    monkeypatch.setattr(rate_limit, "_memory_limiter", rate_limit.InMemoryRateLimiter())
    recorded = []

    async def _record(*_args, **kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(deps, "record_rate_limit_exceeded", _record)
    settings = Settings(rate_limit_backend="memory")
    await deps.check_rate_limit(None, settings, "x", 1, 60)
    with pytest.raises(HTTPException) as exc:
        await deps.check_rate_limit(None, settings, "x", 1, 60)
    assert exc.value.status_code == 429
    assert recorded


def test_rate_limit_key_falls_back_to_client_ip(monkeypatch):
    from apps.api import deps

    monkeypatch.setattr(deps, "get_settings", lambda: Settings(trusted_proxies=""))
    request = SimpleNamespace(client=SimpleNamespace(host="203.0.113.5"), headers={})
    user = SimpleNamespace(id=uuid.UUID(int=7))
    assert deps.rate_limit_key(user, request, "search") == f"search:user:{user.id}"
    assert deps.rate_limit_key(None, request, "search") == "search:ip:203.0.113.5"


def test_rate_limiter_alias_removed():
    from incidentops.security import rate_limit

    assert not hasattr(rate_limit, "limiter")


# ── /ready ───────────────────────────────────


def test_ready_route_returns_only_booleans(monkeypatch):
    from apps.api.routes import health

    async def _detailed():
        return {
            "ready": False,
            "database": "ok",
            "vector_store": "unavailable",
            "required_tables": ["chunks"],
            "required_columns": {"sources": ["name"]},
            "migration": "outdated",
            "current_revision": "old",
            "head_revision": "v2_0002",
            "database_error": "OperationalError: password=hunter2 host=db.internal",
        }

    monkeypatch.setattr(health, "check_database_ready", _detailed)
    app = FastAPI()
    app.include_router(health.router)
    response = TestClient(app).get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body.pop("status") == "not_ready"
    assert body and all(isinstance(value, bool) for value in body.values())
    assert "hunter2" not in response.text
    assert "db.internal" not in response.text


async def test_check_database_ready_does_not_expose_exception_text():
    from incidentops.db.migrations import check_database_ready

    class _BrokenSession:
        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("connection to db.internal failed password=hunter2")

        async def rollback(self):
            return None

    payload = await check_database_ready(_BrokenSession())
    assert payload["ready"] is False
    assert "hunter2" not in repr(payload)
    assert "db.internal" not in repr(payload)


async def test_check_database_ready_can_skip_vector_store(monkeypatch):
    from incidentops.db import migrations

    async def _explode(_self):
        raise AssertionError("vector store must not be checked")

    monkeypatch.setattr(migrations.QdrantVectorStore, "health", _explode)

    class _EmptyResult:
        def all(self):
            return []

        def scalar_one_or_none(self):
            return None

    class _Session:
        async def execute(self, *_args, **_kwargs):
            return _EmptyResult()

    payload = await migrations.check_database_ready(_Session(), skip_vector=True)
    assert payload["vector_store"] == "skipped"


def test_check_migrations_script_accepts_skip_vector(monkeypatch):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "check_migrations.py"
    spec = importlib.util.spec_from_file_location("check_migrations_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.build_parser().parse_args(["--skip-vector"]).skip_vector is True


# ── get_db commit policy ─────────────────────


class _TrackingSession:
    def __init__(self):
        self.new: set = set()
        self.dirty: set = set()
        self.deleted: set = set()
        self.info: dict = {}
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


async def _drive_get_db(monkeypatch, session, mutate=None):
    from incidentops.db import session as session_module

    monkeypatch.setattr(session_module, "_get_session_factory", lambda: (lambda: session))
    generator = session_module.get_db()
    yielded = await generator.__anext__()
    if mutate:
        mutate(yielded)
    with pytest.raises(StopAsyncIteration):
        await generator.__anext__()


async def test_get_db_skips_commit_for_clean_session(monkeypatch):
    session = _TrackingSession()
    await _drive_get_db(monkeypatch, session)
    assert session.commits == 0


async def test_get_db_commits_pending_orm_changes(monkeypatch):
    session = _TrackingSession()
    await _drive_get_db(monkeypatch, session, lambda s: s.new.add(object()))
    assert session.commits == 1


def test_pending_write_tracking_sees_flush_and_core_dml():
    from sqlalchemy import Column, Integer, MetaData, Table, create_engine, insert, select
    from sqlalchemy.orm import Session

    from incidentops.db.session import has_pending_writes

    metadata = MetaData()
    table = Table("t", metadata, Column("id", Integer, primary_key=True))
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with Session(engine) as session:
        session.execute(select(table))
        assert has_pending_writes(session) is False
        session.execute(insert(table).values(id=1))
        assert has_pending_writes(session) is True
        session.commit()
        assert has_pending_writes(session) is False


# ── capabilities / metrics / CORS ────────────


def test_capabilities_limits_and_mcp_feature_follow_settings():
    from apps.api import deps
    from apps.api.routes import capabilities

    app = FastAPI()
    app.include_router(capabilities.router)
    app.dependency_overrides[deps.get_settings_dep] = lambda: Settings(
        mcp_enabled=False, max_documents_per_batch=42, max_batch_bytes=1234
    )
    body = TestClient(app).get("/v1/capabilities").json()
    assert "max_batch_size" not in body["limits"]
    assert body["limits"]["max_batch_bytes"] == 1234
    assert body["limits"]["max_documents_per_batch"] == 42
    assert body["features"]["mcp"] is False
    app.dependency_overrides[deps.get_settings_dep] = lambda: Settings(mcp_enabled=True)
    assert TestClient(app).get("/v1/capabilities").json()["features"]["mcp"] is True


def _metrics_app(settings: Settings):
    from apps.api import deps
    from apps.api.routes import metrics

    app = FastAPI()
    app.include_router(metrics.router)

    async def _db():
        yield None

    app.dependency_overrides[deps.get_db] = _db
    app.dependency_overrides[deps.get_settings_dep] = lambda: settings
    return app


def test_metrics_accepts_scrape_token():
    client = TestClient(_metrics_app(Settings(metrics_token="scrape-secret-123")))
    assert client.get("/metrics", headers={"Authorization": "Bearer scrape-secret-123"}).status_code == 200
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_metrics_scrape_token_disabled_when_unset():
    client = TestClient(_metrics_app(Settings(metrics_token="")))
    assert client.get("/metrics", headers={"Authorization": "Bearer "}).status_code == 401


def test_cors_allows_no_origins_when_unconfigured():
    from apps.api.main import resolve_cors_origins

    assert resolve_cors_origins(Settings(cors_allow_origins="")) == []
    assert resolve_cors_origins(Settings(cors_allow_origins="https://a.example")) == ["https://a.example"]


def test_require_azure_openai_default_matches_deploy_config():
    assert Settings.model_fields["require_azure_openai"].default is False


# ── projects / ingest routes ─────────────────


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _IngestSession:
    def __init__(self, existing=None):
        self.existing = existing
        self.added = []

    async def execute(self, *_args, **_kwargs):
        return _Result(self.existing)

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def refresh(self, _item):
        return None


def _ingest_app(session, settings: Settings, user):
    from apps.api import deps
    from apps.api.routes import ingest

    app = FastAPI()
    app.include_router(ingest.router)

    async def _db():
        yield session

    app.dependency_overrides[deps.get_db] = _db
    app.dependency_overrides[deps.get_settings_dep] = lambda: settings
    app.dependency_overrides[deps.get_current_user] = lambda: user
    return app


def test_create_project_requires_user_outside_production():
    app = _ingest_app(_IngestSession(), Settings(app_env="local"), None)
    response = TestClient(app).post("/v1/projects", json={"name": "demo"})
    assert response.status_code == 401


def test_create_project_conflict_message_is_generic():
    existing = SimpleNamespace(id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4(), email="a@example.com")
    app = _ingest_app(_IngestSession(existing=existing), Settings(app_env="local"), user)
    response = TestClient(app).post("/v1/projects", json={"name": "secret-customer-project"})
    assert response.status_code == 409
    assert "secret-customer-project" not in response.text


def test_local_ingest_uses_window_setting_and_single_batch_event(monkeypatch, tmp_path):
    from apps.api.routes import ingest

    calls: dict[str, list] = {"rate": [], "audit": []}

    async def _rate(*args, **kwargs):
        calls["rate"].append((args, kwargs))

    async def _audit(*_args, **kwargs):
        calls["audit"].append(kwargs["action"])

    async def _noop(*_args, **_kwargs):
        return None

    async def _run(*_args, **_kwargs):
        return {
            "project_id": str(uuid.uuid4()),
            "status": "success",
            "documents_ingested": 1,
            "total_files_seen": 1,
            "files_ingested": 1,
            "files_skipped": 0,
            "chunks_created": 1,
            "duration_ms": 1,
            "source_coverage": {
                "has_logs": True,
                "has_code": False,
                "has_deploys": False,
                "has_incidents": False,
                "has_runbooks": False,
            },
        }

    monkeypatch.setattr(ingest, "check_rate_limit", _rate)
    monkeypatch.setattr(ingest, "record_audit_event", _audit)
    monkeypatch.setattr(ingest, "record_operational_event", _noop)
    monkeypatch.setattr(ingest, "require_project_role", _noop)
    monkeypatch.setattr(ingest, "run_ingestion", _run)
    settings = Settings(
        app_env="local",
        local_ingest_enabled=True,
        local_ingest_allowed_roots=str(tmp_path),
        ingest_rate_limit_window_seconds=1800,
    )
    user = SimpleNamespace(id=uuid.uuid4(), email="a@example.com")
    project = SimpleNamespace(id=uuid.uuid4())
    app = _ingest_app(_IngestSession(existing=project), settings, user)
    response = TestClient(app).post(f"/v1/projects/{project.id}/ingest", json={"path": str(tmp_path)})
    assert response.status_code == 200, response.text
    args, _kwargs = calls["rate"][0]
    assert args[4] == 1800
    assert "documents_batch_ingested" not in calls["audit"]
    assert calls["audit"].count("local_ingest_finished") == 1


# ── dead code ────────────────────────────────


def test_dead_code_removed():
    import importlib

    from apps.api import deps
    from incidentops.schemas import api as schemas
    from incidentops.security import auth

    for name in ("require_viewer", "require_investigator", "require_approver"):
        assert not hasattr(deps, name)
    assert not hasattr(auth, "ensure_seed_admin")
    assert not hasattr(schemas, "HealthResponse")
    for module in ("incidentops.security.sanitizer", "incidentops.db.repositories"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)
