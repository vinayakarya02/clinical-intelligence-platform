# cip_platform — Production Platform Library

Phase 4. Caching, events, background work, observability, security, MLOps, and feature flags.

**A library, not a framework.** It exports protocols and implementations; it starts nothing
and wraps nothing. The dependency rule is one-way and enforced by a test: application code may
import this package, and this package never imports application code
([ADR-0013](../../docs/design/adr-0013-platform-library-boundary.md)).

That constraint is why Phases 1–3 are untouched. Cross-cutting behaviour is applied at the
edges — gateway middleware, protocol adapters, and observers that read a finished answer —
rather than threaded through the reasoning code.

## Modules

| Module | What it does | The decision worth knowing |
|---|---|---|
| `config` | Environment-aware settings | **Refuses** an unsafe deployed configuration at startup, because each of those fails *silently* in production |
| `correlation` | One id through logs, metrics, events, jobs | A ContextVar, so it is per-task under asyncio rather than shared |
| `cache/` | Five domains, one interface | `CacheKey` cannot be built without a tenant |
| `events/` | Typed event spine | The bus emits `AuditLogged` itself, so audit is not something anyone can forget |
| `tasks/` | Six job kinds | Failures are classified; only transient ones retry, and exhausted ones dead-letter visibly |
| `observability/` | Metrics + OTel GenAI conventions | Standard `gen_ai.*` names; a cardinality guard that refuses an unbounded label |
| `security/` | Authn, RBAC, limits, secrets | Spend budgets are separate from rate limits, because they bound different things |
| `mlops/` | Model / eval registries | A compatibility matrix, because individually-approved components can still be an untested combination |
| `flags` | Feature flags | Off, on, or a tenant-stable rollout — nothing more |

## What it deliberately does not do

**It does not instrument the application.** `AnswerObserver` reads a finished `Answer` and
emits telemetry. The copilot has no opinion about Prometheus and the cache has no opinion
about clinical claims.

**It does not own connections.** `RedisCache` takes a constructed client, because pooling,
TLS, and failover belong to whatever builds it.

**It does not implement Kafka or Celery yet.** Protocols and in-memory backends exist; the
in-memory ones are not mocks — they carry the same TTL, retry, idempotency, and dead-letter
semantics — but nothing here has run against a real broker.

## Running the benchmarks

```bash
python scripts/benchmark_platform.py
```

Platform overhead on the request path measures ~33 µs against a ~3.9 ms answer — 0.83% of
end-to-end. That figure is the justification for putting caching, authentication, rate
limiting, and metrics on every request.
