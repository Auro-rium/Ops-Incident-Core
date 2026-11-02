#!/usr/bin/env bash
# Start every process the free Render web service runs. Render injects
# postgresql:// URLs; the app needs the asyncpg driver.
set -euo pipefail

export DATABASE_URL="${DATABASE_URL/#postgres:\/\//postgresql+asyncpg://}"
export DATABASE_URL="${DATABASE_URL/#postgresql:\/\//postgresql+asyncpg://}"
export QDRANT_URL="http://127.0.0.1:6333"
PORT="${PORT:-10000}"

# The container disk is wiped on every restart, so vectors are rebuilt by the seed step.
data_dir="$(mktemp -d)"
(
  cd "$data_dir"
  QDRANT__SERVICE__HOST=127.0.0.1 \
  QDRANT__STORAGE__STORAGE_PATH="$data_dir/storage" \
  QDRANT__STORAGE__ON_DISK_PAYLOAD=true \
  QDRANT__TELEMETRY_DISABLED=true \
  QDRANT__LOG_LEVEL=WARN \
    exec qdrant
) &
qdrant_pid=$!

alembic upgrade head
python -m incidentops.security.bootstrap_admin --email "$BOOTSTRAP_ADMIN_EMAIL" --password "$BOOTSTRAP_ADMIN_PASSWORD"

python -m incidentops.worker &
worker_pid=$!
python scripts/seed_demo.py &

uvicorn apps.api.main:app --host 0.0.0.0 --port "$PORT" --workers 1 &
api_pid=$!

# Any core process dying takes the container down so Render restarts it whole.
wait -n "$qdrant_pid" "$worker_pid" "$api_pid"
exit 1
