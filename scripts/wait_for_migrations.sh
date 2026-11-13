#!/bin/sh
# Bounded wait for the schema, used before the API starts.
#
# Always exits 0. The migration job runs after the deployment finishes, and a
# container that never starts would hold that deployment open, so the API starts
# after the wait and /ready reports 503 until the schema is current.
# azure_run_migrations.sh restarts the API once the job succeeds.
#
# With the defaults the wait ends within 3.5 minutes (deadline plus one capped
# attempt), inside the Container Apps default startup probe window of about 4.

max_seconds="${MIGRATION_WAIT_MAX_SECONDS:-180}"
delay="${MIGRATION_WAIT_SECONDS:-15}"
attempt_timeout="${MIGRATION_WAIT_ATTEMPT_TIMEOUT_SECONDS:-30}"

deadline=$(($(date +%s) + max_seconds))
while :; do
  if timeout "$attempt_timeout" python scripts/check_migrations.py; then
    exit 0
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    break
  fi
  echo "Schema not ready; retrying in ${delay}s" >&2
  sleep "$delay"
done

echo "WARNING: schema still not ready after ${max_seconds}s; starting the API anyway. /ready returns 503 until migrations run." >&2
exit 0
