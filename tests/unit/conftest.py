from __future__ import annotations

import pytest

# CI runs this suite in every deployment mode. Unit tests build their own Settings,
# so the mode of the surrounding stack must not leak into those defaults.
_DEPLOYMENT_MODE_VARS = ("RATE_LIMIT_BACKEND", "WORKER_MODE", "JOB_QUEUE_BACKEND", "RAG_ASYNC_INDEXING", "INDEX_JOB_TIMEOUT_SECONDS")


@pytest.fixture(autouse=True)
def _default_deployment_mode(monkeypatch):
    for name in _DEPLOYMENT_MODE_VARS:
        monkeypatch.delenv(name, raising=False)
