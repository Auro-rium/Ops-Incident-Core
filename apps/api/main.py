"""
IncidentOps API entrypoint.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from incidentops.config.settings import Settings, get_settings
from incidentops.config.validation import validate_startup_settings
from incidentops.db.session import create_tables
from incidentops.observability.metrics import incr, observe_latency
from incidentops.observability.tracing import configure_tracing
from incidentops.retrieval.vector_store import QdrantVectorStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s — %(message)s")
logger = logging.getLogger("incidentops.api")


def should_run_create_all(settings: Settings) -> bool:
    if settings.is_production_like:
        return False
    return settings.normalized_app_env in {"local", "development"} and settings.db_create_all


async def initialize_database_for_startup(settings: Settings) -> None:
    validate_startup_settings(settings)
    if settings.is_production_like:
        vector_store = QdrantVectorStore(settings)
        if not await vector_store.health():
            raise RuntimeError("Qdrant is unavailable")
        await vector_store.ensure_collection(settings.embedding_dim)
    if not should_run_create_all(settings):
        if settings.is_production_like and settings.db_create_all:
            logger.warning("Ignoring DB_CREATE_ALL=true because APP_ENV=%s forbids create_all", settings.app_env)
        return

    await create_tables()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await initialize_database_for_startup(get_settings())
    incr("app_startups")
    yield


app = FastAPI(
    title="IncidentOps Agent",
    version="0.5.0",
    description="Production-style incident investigation copilot for backend and SRE teams.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins_list or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

configure_tracing(get_settings(), app)


@app.middleware("http")
async def observe_http_requests(request: Request, call_next):
    start = time.time()
    try:
        response = await call_next(request)
    except Exception:
        incr("http_requests_total")
        incr("http_errors_total")
        observe_latency("http_request_duration", (time.time() - start) * 1000)
        raise
    latency_ms = (time.time() - start) * 1000
    incr("http_requests_total")
    observe_latency("http_request_duration", latency_ms)
    if response.status_code >= 400:
        incr("http_errors_total")
    return response

from apps.api.routes import (  # noqa: E402
    answer,
    auth,
    capabilities,
    evals,
    health,
    ingest,
    investigate,
    metrics,
    operations,
    readiness,
    runtime,
    runs,
    search,
    sources,
)

app.include_router(health.router)
app.include_router(capabilities.router)
app.include_router(auth.router)
app.include_router(ingest.router)
app.include_router(search.router)
app.include_router(answer.router)
app.include_router(investigate.router)
app.include_router(runs.router)
app.include_router(metrics.router)
app.include_router(operations.router)
app.include_router(readiness.router)
app.include_router(runtime.router)
app.include_router(evals.router)
app.include_router(sources.router)
