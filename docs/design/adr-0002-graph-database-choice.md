# ADR-0002: Neo4j as the Clinical Knowledge Graph Store

**Status:** Accepted (Phase 0)
**Date:** 2026-08-01

## Context

The platform needs a graph database to store clinical entities, ontology-linked concepts, and
relationships, and to serve both local (entity-neighborhood) and global (community-summary)
queries as part of the hybrid retrieval design ([ADR-0001](adr-0001-hybrid-graph-vector-retrieval.md)).

Candidates evaluated: Neo4j, Amazon Neptune (+ Neptune Analytics), TigerGraph (+ TigerVector).

## Decision

**Neo4j** (Aura managed service for cloud tenants, self-hosted Neo4j Enterprise for on-prem/hybrid
hospital deployments) is the primary graph store for Phase 1+.

Rationale:

- Most mature ecosystem for GraphRAG-style patterns specifically (native GraphRAG context
  providers, first-party Microsoft GraphRAG integration guides, native vector index alongside
  full-text and graph traversal in one Cypher query).
- Largest available engineering talent pool and query-language (Cypher) familiarity — relevant
  for a platform that must be maintainable by customer-facing solutions engineers, not just a
  core platform team.
- Neo4j 4.0+ native multi-database support gives a clean per-tenant isolation primitive
  (database-per-tenant for large/regulated accounts) without bolting isolation onto a shared
  graph via application logic alone.
- Aura offers HIPAA-eligible hosting with BAA support, satisfying the compliance baseline in
  [06-security-compliance.md](../architecture/06-security-compliance.md).

## Consequences

- **Positive:** fastest path to a production GraphRAG-pattern implementation; strong hybrid
  vector+fulltext+graph query support in a single system reduces the need for a fourth
  specialized store.
- **Negative:** Neo4j's native vector index is less mature at extreme scale than a dedicated
  vector database or TigerVector's benchmarked hybrid performance; if a tenant's vector workload
  scales past what Neo4j's vector index comfortably serves, the platform still routes bulk
  narrative-text vector search to pgvector/OpenSearch (per [ADR-0001](adr-0001-hybrid-graph-vector-retrieval.md)).
  Neo4j's vector index is scoped to **community summary embeddings only** — see
  [graph-schema.md §7](../database/graph-schema.md#7-constraints--indexes); no per-entity
  embedding property is defined anywhere in the schema, so there is no entity-level vector
  workload on Neo4j to bound in the first place. (An earlier draft of this ADR described the scope
  as "entity/community" — corrected here per
  [Phase 0 review finding C16](phase-0-architecture-review.md) to match what the schema actually
  implements.)
- **Negative:** database-per-tenant has an operational ceiling — Neo4j Aura and self-hosted
  clusters both have practical limits on databases-per-instance, and provisioning/migration
  automation for potentially thousands of tenant databases is not yet designed. Tracked with
  concrete target numbers in [docs/nfr.md](../nfr.md) rather than asserted here without them; HA
  and caching topology for the graph store (read replicas, hot-traversal cache) is specified in
  [deployment-architecture.md §2](../deployment/deployment-architecture.md#2-reference-cloud-architecture-multi-tenant-topology).
- **Revisit trigger:** if a tenant requires population-scale cohort graph analytics (billions of
  edges, heavy parallel graph algorithms) beyond Neo4j's comfortable operating envelope, evaluate
  TigerGraph as a specialized addition for that tenant's analytics workload specifically — not a
  platform-wide migration.

## Alternatives considered

- **Amazon Neptune / Neptune Analytics** — strong AWS-native option with built-in vector search at
  scale; [deployment-architecture.md §3](../deployment/deployment-architecture.md#3-cloud-provider-mapping)
  documents it as an **available alternative for AWS-committed tenants**, not a recommendation —
  Neo4j Aura remains the default across all cloud providers for cloud-portability reasons. (An
  earlier draft of this ADR called Neptune "the recommended choice for AWS-only deployments,"
  which overstated what the deployment doc actually commits to; corrected per
  [Phase 0 review finding A13](phase-0-architecture-review.md).)
- **TigerGraph + TigerVector** — best raw hybrid vector+graph query performance in published
  benchmarks, but smaller ecosystem/talent pool and steeper GSQL learning curve; noted as the
  revisit option above.
- **MongoDB Atlas Vector Search** — evaluated as part of a broader storage-engine comparison, not
  specific to the graph-store decision; see [ADR-0004](adr-0004-storage-engine-evaluation.md).
