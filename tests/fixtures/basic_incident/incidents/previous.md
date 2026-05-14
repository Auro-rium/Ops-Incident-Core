# Incident
## Summary
orders-api previously regressed after a deploy.
## Root Cause
a blocking upstream call increased latency in the request path.
