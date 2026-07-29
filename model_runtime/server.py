"""SageMaker-compatible HTTP server for the pinned GPU reranker image."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

import score


@asynccontextmanager
async def lifespan(_app: FastAPI):
    score.init()
    yield


app = FastAPI(title="IncidentOps Reranker", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/ping")
async def ping() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/invocations")
async def invoke(payload: dict) -> dict:
    try:
        return score.run(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid reranker request") from exc
