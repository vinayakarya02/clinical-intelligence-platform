# ADR-0004: Document/Vector Storage Engine — Postgres+OpenSearch Split vs. Unified Atlas

**Status:** Accepted (Phase 0)
**Date:** 2026-08-01
**Context:** raised by [Phase 0 review finding D20](phase-0-architecture-review.md) — the original
document set chose PostgreSQL (relational + pgvector) plus OpenSearch (BM25) as the document/
chunk/embedding storage tier without ever comparing it against a unified document+vector store.
This ADR performs that comparison.

## Context

[ADR-0001](adr-0001-hybrid-graph-vector-retrieval.md) justifies *why* the platform needs vector,
keyword, and graph retrieval together. It does not justify *which storage engine* serves the
document/chunk/vector tier specifically. The current design splits that tier across three systems
(Postgres for relational facts + pgvector for embeddings, OpenSearch for BM25 keyword search),
synchronized by the shared event pipeline in
[02-rag-hybrid-retrieval.md §1](../architecture/02-rag-hybrid-retrieval.md#1-ingestion--indexing-pipeline)
and tracked via the `index_sync_state` watermark table in
[postgres-schema.sql](../database/postgres-schema.sql). A genuine alternative exists: **MongoDB
Atlas**, which offers Atlas Vector Search (ANN over embedded document arrays) and Atlas Search
(native BM25/Lucene-based full-text) in the same document database, plus Queryable Encryption and
collection-per-tenant patterns that map onto isolation needs already established in
[ADR-0003](adr-0003-multi-tenancy-model.md).

## Comparison

| Dimension | Current: Postgres + pgvector + OpenSearch | Alternative: MongoDB Atlas (unified) |
|---|---|---|
| Number of stores for document/chunk/vector/keyword tier | 3 (Postgres relational, pgvector, OpenSearch) | 1 (Atlas, with Vector Search + Atlas Search as features of the same collection) |
| Sync-drift risk | Real — `index_sync_state` exists specifically to track and detect drift between stores; a sync failure leaves one store stale relative to another | Eliminated for this tier — document, chunks, and embeddings can live in one document with one write path; no cross-store watermark needed |
| Structured relational facts (patients, encounters, conditions, medications, observations) | Natural fit — normalized relational schema, foreign keys, `JOIN`s, RLS | Poor fit — MongoDB is not the right engine for highly relational, foreign-key-heavy clinical facts; would require denormalization or a second store anyway, undermining the "unified" benefit |
| Field-level encryption | Application-layer envelope encryption on specific columns (see [postgres-schema.sql](../database/postgres-schema.sql) PHI-column comments) | Native Queryable Encryption — a real advantage, directly answering the encryption gap in [Phase 0 review finding D2](phase-0-architecture-review.md) |
| Multi-tenancy pattern | Schema-per-tenant / RLS (Postgres), metadata-partitioned + RLS (pgvector) — see [ADR-0003](adr-0003-multi-tenancy-model.md) | Collection-per-tenant or field-level partitioning — a comparably mature pattern, would mirror the same defense-in-depth structure |
| ANN recall/scale at this platform's likely corpus size | Well-understood via pgvector HNSW; tuning parameters specified in [postgres-schema.sql](../database/postgres-schema.sql) | Plausible but unverified at this platform's specific scale/cost point — no internal benchmark exists yet |
| Keyword/BM25 search | OpenSearch — mature, widely used, but a genuinely separate system requiring its own sync | Atlas Search — native Lucene-based, same collection, no separate sync |
| Graph traversal | Not applicable to either option — Neo4j remains required regardless (see [ADR-0002](adr-0002-graph-database-choice.md)) | Same — this ADR does not touch the graph-store decision |
| Team/ecosystem familiarity | Postgres is the default operational-store choice across the rest of the schema; one relational engine to operate | A second database technology (document store) alongside Postgres and Neo4j — three storage paradigms instead of two |

## Decision

**Retain the Postgres + pgvector + OpenSearch split** for Phase 1, rather than migrating to a
unified Atlas store. Rationale:

- The platform's structured clinical facts (`patients`, `encounters`, `conditions`,
  `medications`, `observations`) are inherently relational and foreign-key-heavy — this is the
  dominant data shape for the operational store, not the document/chunk/vector tier, and Postgres
  is the right engine for it regardless of what serves vectors. Adopting Atlas would not eliminate
  a store; it would add a third storage paradigm (document DB, alongside relational Postgres and
  graph Neo4j) rather than reduce from three to two.
- The sync-drift risk this ADR set out to weigh is real but already mitigated architecturally —
  every store in the current design is built from the same ingestion event stream (
  [02-rag-hybrid-retrieval.md §1](../architecture/02-rag-hybrid-retrieval.md#1-ingestion--indexing-pipeline)),
  and `index_sync_state` makes drift *observable and alertable* rather than silent. Consolidating
  onto Atlas would remove this risk for the document/chunk/vector tier specifically, but not for
  the graph store, which stays separate either way — the marginal benefit is smaller than it
  first appears.
- Atlas's Queryable Encryption is a genuine, adopted improvement regardless of this decision — see
  [06-security-compliance.md §9](../architecture/06-security-compliance.md#9-encryption) for how
  the equivalent column-level encryption requirement is now applied to Postgres instead.

## Consequences

- **Positive:** avoids introducing a third storage paradigm; keeps the relational/graph split
  clean along the lines that actually matter (structured facts vs. relationship traversal).
- **Negative:** the platform still operates three synchronized stores for the document/chunk/
  vector/keyword tier (Postgres, pgvector, OpenSearch) rather than one, and must maintain the
  sync-drift detection tooling (`index_sync_state`) this decision accepts as a known, managed cost
  rather than an eliminated one.
- **Revisit trigger:** if Phase 1 benchmarking shows pgvector HNSW recall/latency degrading
  unacceptably at real corpus scale, or if OpenSearch operational overhead proves disproportionate
  to its value over Postgres full-text search, re-open this ADR with actual numbers rather than
  the qualitative comparison above.

## Alternatives considered

- **MongoDB Atlas (unified document + vector + keyword store)** — compared in detail above;
  rejected for Phase 1 as adding a storage paradigm rather than removing one, given the platform's
  relational core.
- **Elasticsearch instead of OpenSearch** — functionally similar for this platform's needs;
  OpenSearch preferred for licensing (Apache 2.0) and to avoid Elastic's dual-licensing terms
  affecting a compliance-sensitive product.
