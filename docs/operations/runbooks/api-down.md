# Runbook: API unreachable

**Alert:** `ApiDown`
**Severity:** critical
**Dashboard:** [CIP Overview](../../../observability/grafana/cip-overview.json)

## What this means

Prometheus cannot scrape API replicas — they are gone, wedged, or unroutable.

## Immediate check

1. `kubectl -n cip get pods -l app.kubernetes.io/name=cip-api`
2. If pods are `Running` but not ready, read the readiness probe output — it names
   the failing dependency.
3. If pods are crash-looping, check for a configuration refusal: `PlatformSettings`
   deliberately refuses to start on an unsafe production configuration.

## Likely causes, most common first

- **A secret is missing or empty.** The Secret manifest ships empty by design, so a
  deployment that did not wire an ExternalSecret fails exactly this way.
- **A dependency is unreachable**, failing readiness. Correct behaviour: the pods
  are removed from the Service rather than killed.
- **An unsafe configuration** was rejected at startup — read the error, it lists
  every problem at once.
- **PodDisruptionBudget blocked a drain**, leaving fewer replicas than expected.

## Mitigation

1. Fix the configuration or secret and let the rollout proceed.
2. Do **not** relax the startup checks to get pods running. They refuse
   configurations that fail silently in production, which is worse than not
   starting.

## What this is *not*

Not usually a code problem if it began at a deploy boundary.
