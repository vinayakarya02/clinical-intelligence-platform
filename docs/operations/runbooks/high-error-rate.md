# Runbook: Error rate above 5%

**Alert:** `HighErrorRate`
**Severity:** critical
**Dashboard:** [CIP Overview](../../../observability/grafana/cip-overview.json)

## What this means

More than 5% of requests are returning 5xx.

## Immediate check

1. Every error response carries a `correlation_id` in the body. Take one from a
   user report and query logs by it — that is the fastest path to the cause.
2. Check whether errors are concentrated in one tenant or route.
3. Distinguish 5xx from 429: rate-limit and budget rejections are *not* errors and
   are excluded from this alert.

## Likely causes, most common first

- **A datastore is failing** in a way readiness does not catch.
- **An unhandled exception** on a specific route — the generic 500 detail carries
  only a type name, so the logs are authoritative.
- **A dependency is timing out** rather than refusing.

## Mitigation

1. Roll back the most recent deployment if the onset matches it.
2. Otherwise follow the correlation id to the failing dependency.

## What this is *not*

Not to be diagnosed from response bodies: internal messages are deliberately
withheld from clients, because they contain identifiers and query fragments.
