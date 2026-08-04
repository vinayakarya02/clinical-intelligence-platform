# Runbook: Answer latency high

**Alert:** `SlowAnswers`
**Severity:** warning
**Dashboard:** [CIP Overview](../../../observability/grafana/cip-overview.json)

## What this means

p95 end-to-end latency is above 10 seconds.

## Immediate check

1. Use the stage-duration panel. The pipeline records every stage, so this
   question is answerable without reproducing anything.
2. Check cache hit rate — a collapse shows up here first.
3. Check whether the `retrieve` or `generate` stage dominates.

## Likely causes, most common first

- **`generate` dominates** — the model provider is slow. Check its status.
- **`execute` dominates** — a tool or datastore is slow.
- **Cache hit rate fell**, so every request pays full cost.
- **CPU saturation.** Check HPA behaviour; the API has no CPU limit precisely so
  that throttling does not masquerade as this.

## Mitigation

1. Scale out if CPU-bound — the HPA should already be doing so; confirm it is not
   at `maxReplicas`.
2. If the provider is slow, there is no local mitigation beyond reducing context
   size or failing fast.
3. Restore the cache if that is the cause.

## What this is *not*

Not a reason to raise timeouts. A longer timeout converts a fast failure into a
slow one and holds a connection while doing it.
