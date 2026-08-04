# ADR-0015: An event spine with audit emitted by the bus

**Status:** Accepted (Phase 4)

## Context

Document processing is a pipeline with genuinely independent consumers: embeddings, the graph,
evaluation, and audit all care about a new chunk and none of them needs to block the others.
Direct calls would couple them and serialise them; an event spine does not.

The harder question is audit. HIPAA §164.312(b) requires a record of access to PHI, and the
usual implementation is a call to an audit function at each point that matters — which is a
requirement satisfied by every developer remembering.

## Decision

Events are published to an `EventBus` protocol. `InMemoryEventBus` runs in-process and in
tests; a Kafka-backed implementation is the production shape.

**The bus emits `AuditLogged` itself, for every event it publishes.** Audit is therefore not a
handler anybody can forget to register, and "was this audited" is a property of the bus rather
than of code review. It also means the audit trail and the event log cannot diverge.

Every envelope carries `event_id`, `tenant_id`, `correlation_id`, `causation_id`, and the W3C
`traceparent`, so a document's whole lifecycle reconstructs as one trace across processes.

Delivery is at-least-once and consumers are idempotent by `event_id`. Exactly-once across a
queue and a database needs distributed transactions; a duplicate embedding write is harmless
where a lost one is silent data loss.

## Consequences

- A slow consumer cannot block a producer, and a failing one cannot lose the event.
- Ordering holds per partition key (the tenant), not globally. Cross-tenant ordering is not
  something any consumer here needs.
- The in-memory bus dispatches synchronously, so test failures surface at the publish call
  rather than asynchronously. Production behaviour differs, and integration tests are the only
  place that difference is visible.
