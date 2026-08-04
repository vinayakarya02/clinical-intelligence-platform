# Runbook: Background jobs dead-lettering

**Alert:** `DeadLetterQueueGrowing`
**Severity:** critical
**Dashboard:** [CIP Overview](../../../observability/grafana/cip-overview.json)

## What this means

Jobs have exhausted their retries. Documents are not being ingested, embedded, or
indexed — which surfaces to clinicians as missing evidence, not as an error.

## Immediate check

1. Break down by `job_kind`. Ingest failures are the most urgent.
2. Read the dead-letter errors — the classification tells you whether retrying
   could ever have helped.
3. Check whether the failures are `PermanentTaskError` (payload) or exhausted
   `TransientTaskError` (dependency).

## Likely causes, most common first

- **A dependency is down.** Exhausted transient errors, all the same type.
- **A malformed payload.** Permanent errors, usually one producer.
- **A handler is unregistered.** Dead-lettered immediately with
  "no handler registered" — a deployment wiring error.
- **Visibility timeout shorter than the job.** Two workers process the same task
  and one loses; check `CIP_QUEUE_VISIBILITY_TIMEOUT` against job duration.

## Mitigation

1. Fix the dependency, then re-enqueue. Jobs carry idempotency keys, so
   re-enqueueing is safe.
2. For permanent failures, fix the producer — re-enqueueing will fail identically.
3. Never clear the dead-letter queue without reading it. A cleared queue is
   indistinguishable from work that was never submitted.

## What this is *not*

Not something to resolve by increasing `max_retries`. A permanent failure retried
more times is the same failure later.
