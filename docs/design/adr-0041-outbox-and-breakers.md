# ADR-0041: Transactional outbox for events, circuit breakers for every dependency

**Status:** Accepted (Phase 9, W7)

## Context

W0 gave the platform real remote dependencies: PostgreSQL, MongoDB, Neo4j, Redis, and Kafka. W1
will make them carry the data. Two problems become live at that moment, and both are cheaper to
solve before the persistence code exists than to retrofit through every write path afterwards.

**Dual writes.** `IntegrationEngine.ingest()` (`services/interop/.../routing.py:428`) stores FHIR
resources, registers EMPI aliases, then publishes a clinical event. Today all three are in-memory
and it is *accidentally* atomic. Once the repositories are PostgreSQL and the stream is Kafka, a
failure between the store and the publish leaves a resource that exists and a downstream world
that never hears about it — silently, with the caller already told the document was accepted.

**Retry storms.** The platform has retries (84 sites), backoff, idempotency, dead-letter
handling, and timeouts. It has no circuit breakers. Three replicas each retrying three times
against a database that has become *slow* rather than down is nine times the load on the thing
already struggling.

## Decision

### The outbox

Business data and the intent to publish are written in **one local transaction**. A separate
relay carries the intent to the broker afterwards.

This is the standard answer because the alternatives do not work. Kafka transactions are
Kafka-to-Kafka and do not span a database. Two-phase commit across Postgres and Kafka exists in
theory and is operated by almost nobody. The outbox converts an unsolvable distributed-transaction
problem into an ordinary one: rows in a table, delivered at-least-once, with idempotent consumers.

**A polling relay, not CDC.** Debezium would give millisecond latency and cost logical
replication, connector operations, restart handling, and schema-evolution management — for a
platform with no staging cluster. Polling trades latency for operational simplicity, and the
schema is CDC-compatible, so the migration is available later without changing it.

**Ordering is per partition, and only the head is eligible.** The claim query takes
`DISTINCT ON (partition_key)` — the unpublished row with the lowest `sequence_id` for each key —
and applies the `next_attempt_at` filter *outside* the CTE. Filtering inside would let a later
row become the "head" while the true head sits in backoff, publishing an admission after its own
discharge. That is one line away from being reintroduced, which is why an integration test
targets it directly.

**The lock is the claim.** `FOR UPDATE SKIP LOCKED`, with no `status = 'publishing'` column. Two
relays take disjoint rows; without `SKIP LOCKED` they serialise, without `FOR UPDATE` they
duplicate. And a status column set by a process that then dies leaves a row nothing reclaims —
not pending, so no relay takes it; not published, so nothing completes it. A row lock is released
by the database when the connection dies, which is crash recovery for free.

**Two identifiers, not interchangeable.** `event_id` (UUID) deduplicates and is stable across
every redelivery. `sequence_id` (bigserial) orders — UUIDs do not order, and `created_at` cannot
be trusted for it, because two rows written in the same millisecond on different connections have
no defined relative order and clock skew makes timestamps actively wrong.

### Circuit breakers

Built in `cip_platform.resilience` rather than adopted. `pybreaker` is thread-oriented;
`aiobreaker` is a lightly-maintained fork. Neither implements a **sliding failure window** or a
**half-open success threshold**, and those are the two properties that make a breaker useful:
consecutive-failure counters flap, and closing after one successful probe reopens the circuit onto
a dependency that answered once by luck.

**One breaker per dependency.** A shared breaker would let a slow knowledge graph open the circuit
in front of the operational database, converting a degraded feature into an outage — the exact
failure a breaker exists to prevent.

**The guarded call composes them in one fixed order:** `retry(breaker(timeout(operation)))`. The
timeout is innermost so a hang becomes a countable failure; the breaker is inside the retry so a
rejected call is never retried, which is the storm the breaker was opened to stop.

## Consequences

**At-least-once, and the duplicate is bounded by three stacked mechanisms**: an idempotent Kafka
producer, marking the row published only after the broker acknowledges, and `eventId` on every
message. Remove any one and a bounded duplicate becomes either a lost event or an unbounded one.
Consumers must be idempotent. This is not a caveat; it is the contract.

**Latency.** Events arrive milliseconds to seconds after commit rather than synchronously. For a
clinical event stream that is well inside tolerance.

**The RLS policy on `outbox_events` is a guard, not a boundary.** The relay is cross-tenant by
nature — one process drains every tenant — so it cannot set a single `app.tenant_id`. It opts into
a relay policy via a GUC, set with `SET LOCAL` so it cannot leak onto a pooled connection. The
application role can set the same GUC, so this prevents *accidental* cross-tenant reads and not a
determined one. **Production should run the relay under its own database role holding BYPASSRLS**;
that is the real boundary, and it is deployment configuration rather than code.

**A second publishing path would silently void the guarantee.** Anything that publishes without
going through the outbox is a dual write again, and every test would still pass.

## Alternatives considered

**Debezium / CDC.** Deferred, not rejected — the schema is compatible and the migration needs no
data change. Revisit when sub-second event latency is a requirement or polling load becomes
measurable.

**Kafka transactions.** Do not span a database. They solve consume-transform-produce, which is not
this problem.

**Saga instead of outbox.** A different tool for a different question. Sagas coordinate a
multi-service *business* transaction with compensating actions; the outbox makes one local write
and one publish atomic. This platform needs the second. When cross-service workflows need
rollback semantics, a saga sits *on top* of reliable event delivery rather than replacing it.

**Two-phase commit.** Requires an XA-capable broker and a transaction coordinator, and blocks
resources during the prepare phase. Kafka does not support XA.
