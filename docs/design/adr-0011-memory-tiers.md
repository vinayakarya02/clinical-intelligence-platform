# ADR-0011: Three memory tiers with explicit promotion

**Status:** Accepted (Phase 3)

## Context

Multi-turn clinical conversation needs "the patient" and "that lab" to carry across turns
without re-sending the whole history to the model each time, and without a summary quietly
becoming the system of record for a clinical fact.

The agent-memory literature converges on a layered split: working memory for the live turn,
episodic memory for compact session summaries, and semantic memory for structured entity-level
facts.

## Decision

Three tiers with explicit, testable promotion rules:

| Tier | Holds | Lifetime | Promotion |
|---|---|---|---|
| Working | the last N turns verbatim | one session, bounded | evicted oldest-first |
| Episodic | compact summaries of evicted turns | one session | written on eviction |
| Semantic | entities and their referents (patient, encounter, drug) | session, tenant-scoped | on entity mention |

Two rules make this safe rather than merely useful:

**Memory resolves references; it never supplies evidence.** "What about his creatinine?"
resolves `his` to a patient id from semantic memory, and the creatinine value is then
*retrieved*. A value recalled from a summary is never cited. Summaries are lossy and a lossy
lab value is a wrong lab value.

**Memory is tenant- and session-scoped at construction.** A memory store cannot be built
without a tenant, mirroring `VectorQuery` in Phase 2.

## Consequences

- Summarisation quality affects *reference resolution*, not factual accuracy.
- Cross-session memory is deliberately out of scope: it is a PHI-retention decision with
  consent and audit implications, not an engineering convenience.
- The working-memory bound is a configured token budget, so long conversations degrade
  predictably instead of overflowing the context window.
