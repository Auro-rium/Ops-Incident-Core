"""
IncidentOps API entrypoint.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from incidentops.config.settings import Settings, get_settings
from incidentops.config.validation import validate_startup_settings
from incidentops.db.session import create_tables
from incidentops.observability.metrics import incr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s — %(message)s")
logger = logging.getLogger("incidentops.api")


def should_run_create_all(settings: Settings) -> bool:
    if settings.is_production_like:
        return False
    return settings.normalized_app_env in {"local", "development"} and settings.db_create_all


async def initialize_database_for_startup(settings: Settings) -> None:
    validate_startup_settings(settings)
    if not should_run_create_all(settings):
        if settings.is_production_like and settings.db_create_all:
            logger.warning("Ignoring DB_CREATE_ALL=true because APP_ENV=%s forbids create_all", settings.app_env)
        return

    from sqlalchemy import text

    from incidentops.db.session import _get_engine

    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
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

from apps.api.routes import answer, auth, evals, health, ingest, investigate, metrics, runs, search, sources  # noqa: E402

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(ingest.router)
app.include_router(search.router)
app.include_router(answer.router)
app.include_router(investigate.router)
app.include_router(runs.router)
app.include_router(metrics.router)
app.include_router(evals.router)
app.include_router(sources.router)
