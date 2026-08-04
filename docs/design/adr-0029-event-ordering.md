# ADR-0029: Events are ordered per resolved person, by source sequence, and consumers are idempotent

**Status:** Accepted (Phase 6)

## Context

Phase 4's event spine partitions by tenant, which is all a single-organisation document
pipeline needs ([ADR-0015](adr-0015-event-spine.md)). Clinical events are different: an ADT
discharge that overtakes its own admission produces a patient who is discharged from an
encounter that never started, and a lab result that overtakes its correction reinstates a value
a lab has already retracted.

Total ordering across a whole tenant would solve this and does not scale — it is one partition,
one consumer, for an entire hospital network.

## Decision

**Partition by resolved person id.** Every event about one patient is totally ordered. Across
patients there is no ordering and nothing may depend on one.

**Order by source sequence, not wall clock.** Sending systems' clocks disagree, sometimes by
hours, and `MSH-7` comes from a machine this platform does not administer. Each event carries
the source system's own sequence (`MSH-10` control id plus a per-source monotonic counter);
comparisons use that. Wall-clock time is retained for display and never for ordering.

**Consumers must be idempotent, and the bus assumes redelivery.** At-least-once delivery is the
only guarantee an infrastructure can actually make cheaply, so exactly-once is implemented where
it belongs — in the consumer, as a processed-event ledger keyed on event id. A consumer that
cannot state its idempotency key is not registered.

**A merge is an ordered event in the surviving partition.** When the EMPI merges two people,
their two histories were each ordered and are not ordered relative to each other. The merge
event marks that seam, so a consumer replaying the partition can see that everything before it
is a union of two sequences rather than one sequence.

## Consequences

- Cross-patient analytics must not assume stream order. Population queries read the repository,
  which has a consistent view, rather than the stream, which does not.
- Partition skew is real: a high-volume patient (an ICU stay generating continuous
  observations) is one partition and cannot be parallelised. Accepted — the alternative is
  giving up per-patient ordering, and per-patient ordering is the one that matters clinically.
- Out-of-order arrivals are detected and reported rather than silently reordered. A gap in a
  source sequence means a message was lost in transit, and that is an interface incident, not a
  buffering problem to hide.
- Replay is bounded by retention. Replaying a partition re-delivers to idempotent consumers
  safely, which is what makes reprocessing after a mapping fix possible at all.
