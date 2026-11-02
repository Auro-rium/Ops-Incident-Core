#!/bin/sh
# Bounded wait for the schema, used before the API starts.
#
# Always exits 0. The migration job runs after the deployment finishes, and a
# container that never starts would hold that deployment open, so the API starts
# after the wait and /ready reports 503 until the schema is current.
# azure_run_migrations.sh restarts the API once the job succeeds.
#
# The default wait (3 minutes) stays inside the Container Apps default startup
# probe window of about 4 minutes.

attempts="${MIGRATION_WAIT_ATTEMPTS:-12}"
delay="${MIGRATION_WAIT_SECONDS:-15}"

attempt=1
while [ "$attempt" -le "$attempts" ]; do
  if python scripts/check_migrations.py; then
    exit 0
  fi
  echo "Schema not ready (attempt $attempt/$attempts); retrying in ${delay}s" >&2
  attempt=$((attempt + 1))
  sleep "$delay"
done

echo "WARNING: schema still not ready after $attempts attempts; starting the API anyway. /ready returns 503 until migrations run." >&2
exit 0
