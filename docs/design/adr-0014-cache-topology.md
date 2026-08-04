# ADR-0014: Five cache domains, one interface, tenant in every key

**Status:** Accepted (Phase 4)

## Context

Five things are worth caching — embeddings, retrieval results, sessions, prompts, and graph
traversals — and they have almost nothing else in common. Their keys, lifetimes, invalidation
triggers, and consequences-of-staleness differ by orders of magnitude: a stale prompt is a
minor quality regression, a stale retrieval result for a patient whose record just changed is
a clinical error.

## Decision

One `Cache` protocol, five `CacheDomain` configurations. A domain owns its namespace, its TTL,
and its invalidation trigger; the interface owns serialisation, key construction, and metrics.

**`CacheKey` cannot be constructed without a tenant.** A cache is the easiest place in a
multi-tenant system to leak data, because a key collision produces a silent hit that looks
exactly like a correct one — no error, no log, just another tenant's answer. Making the tenant
a required constructor argument is the same defence `VectorQuery` uses in Phase 2.

Embedding entries are tenant-scoped even though embeddings of identical text are mathematically
identical and a shared cache would be *correct*. A shared entry means one tenant's cache hit
reveals that another tenant holds that exact clinical text — a timing side channel over PHI.
The hit-rate loss is worth less than the isolation.

## Consequences

- Cross-tenant embedding reuse is given up. For a platform whose tenants are separate hospitals
  this is the right trade; for a single-tenant deployment it is pure cost, and the domain
  config is where that would be changed.
- Invalidation is by namespace sweep, not by dependency graph. Ingesting one document
  invalidates a tenant's whole retrieval namespace. Precise invalidation would need a
  document→query index nobody maintains, and an over-broad sweep costs latency where a missed
  one costs correctness.
- Redis is the production backend and an in-memory implementation with identical semantics
  runs in tests, so cache-related bugs fail in CI rather than only under load.
