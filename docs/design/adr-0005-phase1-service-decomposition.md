# ADR-0005: Phase 1 Service Decomposition and MongoDB's Role

**Status:** Accepted (Phase 1)
**Date:** 2026-08-02
**Supersedes nothing. Refines:** [ADR-0004](adr-0004-storage-engine-evaluation.md)

## Context

Phase 0 designed nine services ([01-system-architecture.md §5](../architecture/01-system-architecture.md#5-service-inventory)).
Phase 1 implements the document-intelligence pipeline only. Two decisions had to be made
before writing code:

1. **How much of the nine-service decomposition to build now.** Standing up nine
   deployable services to implement one pipeline would mean nine deployment
   configurations, nine health endpoints, and eight network hops for a workflow that is a
   single sequential transform.
2. **What MongoDB actually stores.** The Phase 0 stack listed MongoDB, but
   [ADR-0004](adr-0004-storage-engine-evaluation.md) had already decided *against* Atlas
   for the chunk/vector tier, leaving Mongo's role undefined.

## Decision

### 1. One deployable service, module boundaries drawn on the target seams

Phase 1 ships **one** deployable unit (`services/ingestion`) plus a shared library
(`libs/cip_core`). Inside it, the Phase 0 service boundaries exist as module boundaries
with no shared state and no cross-imports except through explicit interfaces:

| Phase 0 service | Phase 1 module |
|---|---|
| Ingestion Service | `cip_ingestion.parsers` + `cip_ingestion.validation` |
| Extraction & Coding Service | `cip_ingestion.processing` (ontology coding deferred — see Consequences) |
| Embedding Service | `cip_ingestion.processing.chunking` (chunking only; embedding is Phase 2) |
| Retrieval / Conversational AI / Analytics | not implemented |
| Audit & Compliance Service | `cip_core.audit` + `cip_ingestion.repositories.audit` |
| Identity & Access Service | `cip_ingestion.api.security` (verification half only) |

The pipeline stages communicate through frozen dataclasses
(`cip_ingestion.domain`), not shared mutable state, so extracting a module into its own
service later is a transport change — replace a function call with an event — rather than
a redesign.

The event bus from Phase 0 is deliberately **not** used in Phase 1. A Kafka topic between
two functions in the same process buys nothing and costs a broker to operate; it is
introduced when the first consumer actually runs in a separate process.

### 2. MongoDB stores the parsed-document artifact

Mongo holds the parser's output — pages, blocks, layout kinds, per-page OCR confidence —
keyed by `(tenant_id, document_id)`.

This is a genuine document-store fit rather than a use invented to justify the dependency:
the payload is deeply nested, its shape varies by source format, and it is written once
and read whole. Modelling it relationally would mean a `pages` table and a `blocks` table
joined on every read to reconstruct a structure that is never queried by parts.

It also earns its place operationally: re-chunking a corpus after a strategy change reads
the stored parse instead of re-running OCR, which dominates pipeline cost by a wide margin.

This does not contradict [ADR-0004](adr-0004-storage-engine-evaluation.md), which
evaluated Atlas as the store for **chunks and vectors** and kept Postgres. Chunks remain
in Postgres; only the pre-chunking artifact lives in Mongo.

## Consequences

- **Positive:** one deployment, one migration path, one health endpoint for Phase 1;
  module boundaries already drawn where services will later split; Mongo has a defined,
  defensible role instead of being a listed-but-unused dependency.
- **Negative:** the single service will need to be split before any of its modules can
  scale independently — OCR is CPU-bound and would benefit from separate scaling well
  before the API does. The split is anticipated, not free.
- **Negative:** ontology coding (SNOMED/ICD/LOINC/RxNorm via UMLS) is named in Phase 0 as
  part of the Extraction & Coding Service but is **not** implemented in Phase 1. It
  depends on licensed terminology data ([ontology-licensing.md](../legal/ontology-licensing.md))
  that must be procured first. Section detection and metadata extraction ship; concept
  coding does not. The `LocalConcept` fallback designed in
  [graph-schema.md](../database/graph-schema.md) is what unmapped entities will use when
  coding does land.
- **Revisit trigger:** when Phase 2 adds the embedding worker, re-evaluate whether it runs
  in-process or as the first extracted service. OCR/embedding CPU characteristics differ
  enough from the API's that this is the natural first split.

## Alternatives considered

- **Nine services from the start** — rejected: eight network hops and nine deployment
  configurations to implement one sequential transform, with no independent scaling need
  yet demonstrated.
- **No MongoDB in Phase 1** — considered seriously, since nothing in Phase 1 strictly
  *requires* the parsed artifact to be persisted. Rejected because reprocessing without it
  means re-running OCR over the entire corpus, and the cost of discovering that after a
  corpus has been loaded is much higher than the cost of writing the artifact now.
