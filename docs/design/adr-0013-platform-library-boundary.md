# ADR-0013: A platform library the application calls, not a framework that calls it

**Status:** Accepted (Phase 4)

## Context

Phase 4 adds caching, events, background work, observability, security, and MLOps to a
functionally complete AI platform that must not be redesigned. There are two ways to add
cross-cutting concerns to existing code: a framework the application runs inside, or a library
the application calls.

A framework would mean the copilot pipeline becomes a set of handlers registered with an
orchestrator that owns tracing, retries, and caching. That is how most platform layers are
built, and it would require rewriting Phase 3's stage pipeline — the thing this phase is
explicitly forbidden to redesign, and the thing whose fixed control flow is a deliberate
clinical-safety property (ADR-0009).

## Decision

`cip_platform` is a library. It exports protocols and implementations; it starts nothing and
wraps nothing. Cross-cutting behaviour is applied in three places only:

- **Gateway middleware** — correlation, authn, authz, rate limiting, budget, metrics. Every
  request passes through it, so nothing has to remember to call it.
- **Adapters** — a cache-backed `EmbeddingCache` satisfies Phase 2's existing protocol, so
  caching is configuration rather than a code change in the retrieval service.
- **Observers** — the metrics layer *reads* a finished `Answer` and emits telemetry. The
  copilot does not instrument itself and does not import the platform.

## Consequences

- Phases 1–3 are untouched, and the dependency direction is one-way: application code may
  import the platform; the platform never imports application code. A test enforces it.
- Instrumentation is coarse-grained — per request and per stage, not per function. That is a
  real loss of resolution, accepted because the alternative is editing the reasoning code.
- A caller who bypasses the gateway bypasses the middleware. The gateway is therefore the only
  supported entry point, and that has to be an operational rule rather than a compiler-checked
  one.
