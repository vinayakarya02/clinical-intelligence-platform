# ADR-0007: MongoDB Atlas Vector Search as the Vector Tier

**Status:** Accepted (Phase 2)
**Date:** 2026-08-03
**Supersedes:** the vector-tier portion of [ADR-0004](adr-0004-storage-engine-evaluation.md). The
rest of ADR-0004 — relational facts stay in PostgreSQL, keyword search stays in OpenSearch —
still holds.

## Context

ADR-0004 evaluated MongoDB Atlas against Postgres + pgvector for the chunk/vector tier and kept
Postgres. Its central argument was:

> Adopting Atlas would not eliminate a store; it would add a third storage paradigm (document DB,
> alongside relational Postgres and graph Neo4j) rather than reduce from three to two.

**That premise no longer holds.** Phase 1 adopted MongoDB for the parsed-document artifact store
([ADR-0005](adr-0005-phase1-service-decomposition.md)) on independent grounds. The document
paradigm is already in the stack, already deployed, already operated. The marginal cost of using
it for vectors is now the cost of one more collection, not one more database technology.

ADR-0004 also named an explicit revisit trigger — pgvector recall/latency at real corpus scale —
which has *not* fired, because no corpus has been indexed yet. So this decision is being revisited
on the paradigm-count argument having lapsed, not on measured pgvector deficiency.

## Decision

**MongoDB Atlas Vector Search is the vector tier.** Chunk embeddings are stored in a
`chunk_embeddings` collection alongside `parsed_documents`, and queried via the `$vectorSearch`
aggregation stage.

Vector access sits behind a `VectorStore` protocol with two implementations:

- **`MongoAtlasVectorStore`** — production. Uses `$vectorSearch` with pre-filtering on
  `tenant_id`, `embedding_model_id`, and document metadata.
- **`InMemoryVectorStore`** — exact brute-force cosine search for local development, tests, and
  small tenants. Not a mock: it implements the same protocol with the same filter semantics, so
  the retrieval pipeline is exercised identically in tests. It is not a production option and the
  configuration refuses it in deployed environments.

Two implementations are justified rather than speculative: Atlas Vector Search is an *Atlas-only*
feature, unavailable in a local `mongod`. Without a local implementation there is no way to run
the retrieval pipeline on a developer machine or in CI, which would push every retrieval bug to a
cloud-only environment. The protocol also keeps the Pinecone/Weaviate/Milvus door open, but that
is a secondary benefit — the local/production split alone pays for the interface.

## Consequences

- **Positive:** one fewer paradigm than the Phase 1 design implied — vectors and parsed artifacts
  share a store, a connection pool, and an operational runbook. Atlas Queryable Encryption becomes
  available for embedded chunk text, addressing the column-encryption requirement in
  [06-security-compliance.md §9](../architecture/06-security-compliance.md#9-encryption) with a
  database-native mechanism rather than application-layer envelope encryption.
- **Positive:** Atlas pre-filtering applies `tenant_id` *inside* the vector index rather than
  post-filtering ANN results. That is the correctness property the Phase 0 review demanded
  (finding D5): post-filtering an ANN top-K can silently return zero results for a tenant whose
  documents were crowded out of the candidate set by another tenant's.
- **Negative:** the platform now depends on Atlas specifically, not MongoDB generally. A
  self-hosted or on-prem deployment (a real requirement for some hospital tenants — see
  [deployment-architecture.md §1](../deployment/deployment-architecture.md#1-deployment-topologies))
  cannot use `$vectorSearch`. Those deployments fall back to `InMemoryVectorStore` only at
  corpus sizes where brute force is viable, or require pgvector to be reinstated for that
  topology. **This is an unresolved gap, not a solved problem**, and it is the strongest argument
  against this decision.
- **Negative:** `chunk_embeddings_*` tables and their HNSW indexes in
  [postgres-schema.sql](../database/postgres-schema.sql) become unused. They are retained rather
  than dropped, because the on-prem gap above may require them; leaving them costs an unused table
  and avoids a migration if the gap forces a reversal.
- **Revisit trigger:** if an on-prem tenant with a corpus too large for brute-force search is
  onboarded, this decision must be reopened — either by reinstating pgvector for that topology
  (making the `VectorStore` protocol carry a third implementation) or by requiring Atlas
  connectivity, which some tenants will refuse.

## Alternatives considered

- **Keep pgvector (ADR-0004's decision)** — still defensible, and strictly better for on-prem.
  Overridden because the paradigm-count argument that drove ADR-0004 has lapsed and Atlas's
  in-index tenant pre-filtering is a genuine correctness advantage over post-filtered ANN.
- **Both, selected per deployment topology** — the honest answer to the on-prem gap, and where
  this likely lands. Deferred rather than adopted now: a second production vector backend needs a
  second set of operational runbooks, index-tuning knowledge, and eval baselines, and there is no
  on-prem tenant yet to justify carrying that cost.
