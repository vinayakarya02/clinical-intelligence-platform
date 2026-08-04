# Runbook: Cache hit rate collapsed

**Alert:** `CacheHitRateCollapsed`
**Severity:** warning
**Dashboard:** [CIP Overview](../../../observability/grafana/cip-overview.json)

## What this means

Hit rate is below 20%. This matters more than it looks: a cache that has silently
*stopped working* looks exactly like a cache with a bad hit rate, and only the
error counter distinguishes them.

## Immediate check

1. Check the cache error rate first. Non-zero errors mean the backend is
   unreachable and the cache is failing open, which is the designed behaviour.
2. Check Redis memory and eviction counters.
3. Check whether an embedding or prompt version changed — a version change moves
   every key and produces exactly this pattern for one TTL period.

## Likely causes, most common first

- **A version change moved every key.** Self-healing within one TTL; confirm the
  rate recovers rather than acting.
- **Redis is unreachable.** Errors non-zero, latency up.
- **Redis is evicting under memory pressure.** `allkeys-lru` is correct for a
  cache-only instance, but the working set no longer fits.
- **Over-aggressive invalidation.** A write-heavy tenant sweeping its retrieval
  namespace on every ingest.

## Mitigation

1. If Redis is down, the system is *correct but slow*. Restore Redis; do not
   restart application pods, which would lose nothing and add churn.
2. If evicting, raise `maxmemory` — keeping it below the container limit so Redis
   evicts on its own policy rather than being OOM-killed.

## What this is *not*

Not a correctness problem. Every cache miss is served from the source of truth;
the answers are right, they cost more.
