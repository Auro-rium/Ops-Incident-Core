# Single container for Render's free web service: API, worker and an in-container
# Qdrant. Postgres and Key Value are separate free Render resources.
FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.12.21

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

COPY apps ./apps
COPY incidentops ./incidentops
RUN uv sync --frozen --no-dev --no-editable --no-cache

RUN curl -fsSL https://github.com/qdrant/qdrant/releases/download/v1.12.4/qdrant-x86_64-unknown-linux-musl.tar.gz \
    | tar -xz -C /usr/local/bin qdrant

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin incidentops
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /usr/local/bin/qdrant /usr/local/bin/qdrant

COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY scripts ./scripts
COPY tests/fixtures/basic_incident ./seed/basic_incident

USER incidentops

CMD ["bash", "scripts/render_start.sh"]
