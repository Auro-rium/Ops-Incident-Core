FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Versions come from uv.lock, so rebuilds of a commit get the same dependencies.
RUN pip install --no-cache-dir uv==0.12.21

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

COPY apps ./apps
COPY incidentops ./incidentops
RUN uv sync --frozen --no-dev --no-editable --no-cache

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin incidentops
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv

COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY scripts ./scripts

USER incidentops

EXPOSE 8000 8080

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
