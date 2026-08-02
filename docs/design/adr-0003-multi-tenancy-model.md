# ADR-0003: Defense-in-Depth Multi-Tenancy Model

**Status:** Accepted (Phase 0)
**Date:** 2026-08-01

## Context

CIP is B2B SaaS serving hospitals and pharmaceutical companies, many bound by BAAs that
contractually require demonstrable data isolation. A single isolation mechanism (e.g.,
application-level `tenant_id` filtering alone) is a well-documented failure mode: one missed
`WHERE tenant_id = ?` clause anywhere in the codebase leaks PHI across tenants.

## Decision

Tenant isolation is enforced at multiple independent layers so that a failure in any single layer
does not by itself cause cross-tenant data exposure:

| Store | Isolation mechanism | Notes |
|---|---|---|
| PostgreSQL (operational store) | **Schema-per-tenant** for large/regulated accounts (hospitals, pharma); **shared schema + Row-Level Security** for smaller accounts, enforced by Postgres itself via `current_setting('app.tenant_id')` session variable set per connection | RLS survives a buggy application query; schema-per-tenant satisfies contractual physical-separation requirements. See [postgres-schema.sql](../database/postgres-schema.sql). |
| Vector index (pgvector / OpenSearch) | `tenant_id` denormalized directly onto every embedding row, with Row-Level Security enforcing the same query-planner-level pushdown as the relational tables — not a join-based or metadata-only filter | An earlier draft of this schema only reached tenant scoping transitively via a join to `document_chunks`, which meant a direct query against the embeddings table bypassed isolation entirely; corrected per [Phase 0 review finding D5](phase-0-architecture-review.md) — see [postgres-schema.sql](../database/postgres-schema.sql) `chunk_embeddings_*` tables. |
| Graph store (Neo4j) | **Database-per-tenant** (Neo4j 4.0+ multi-database) for large accounts; `tenant_id` node property + query-time filter enforced by the Retrieval Service's query-building layer for smaller shared-database tenants | See [ADR-0002](adr-0002-graph-database-choice.md). |
| Object storage | Per-tenant bucket prefix + IAM policy scoping (not filename convention alone) | IAM-enforced, not just path-convention. |
| LLM context assembly | Retrieval Service only ever resolves document/entity IDs that passed tenant + RBAC filtering upstream; the Conversational AI Service has no independent data access path | Prevents the most common real-world RAG leak: an unfiltered retriever surfacing another tenant's chunks into an LLM prompt. |

Every cross-store query in the Retrieval & Orchestration Service is required to carry a
`(tenant_id, actor_scopes)` context object threaded from the authenticated request; there is no
code path that queries a store without it (enforced by service-level middleware, not per-handler
discipline).

## Consequences

- **Positive:** satisfies enterprise procurement/BAA requirements for physical separation on
  large accounts while keeping shared-infrastructure economics for smaller ones; no single missed
  filter clause causes a cross-tenant leak.
- **Negative:** schema-per-tenant and database-per-tenant increase operational complexity
  (migrations must run per-schema/per-database; connection pooling must be tenant-aware).
- **Mitigation — connection pooling strawman (not deferred to an unspecified "Phase 1 concern"):**
  standard transaction-pooled PgBouncer cannot safely rotate `search_path`/`app.tenant_id` per
  checkout across hundreds of tenant schemas. The design is a **tenant-sharded pooler tier**: N
  pooler instances, each session-pooling (not transaction-pooling) a fixed shard of tenant
  schemas, with a routing layer mapping `tenant_id → shard` at the API Gateway before a request
  reaches Postgres. Shard count scales with tenant count, not request volume, so it grows
  predictably. This was flagged as under-specified in
  [Phase 0 review finding D12](phase-0-architecture-review.md); concrete shard-sizing numbers are
  tracked in [docs/nfr.md](../nfr.md) once real per-tenant connection counts are measured in
  Phase 1.
- **Scale ceiling:** at 10x tenant growth, schema-per-tenant migration fan-out time and the
  pooler-shard count both grow roughly linearly and are believed manageable; at 100x, per-tenant
  Neo4j database count (see [ADR-0002](adr-0002-graph-database-choice.md)) is the more likely
  first ceiling, since Neo4j Aura/self-hosted clusters have a practical limit on
  databases-per-instance well below what 100x tenants on a single cluster would require. The
  mitigation at that point is horizontal sharding of tenants across multiple Neo4j clusters (a
  routing-layer change, not a re-architecture) — see [docs/nfr.md](../nfr.md) for the numbers this
  is tracked against and the point at which it becomes an active Phase 4/5 workstream rather than
  a theoretical concern.
- **Migration tooling:** centralized in the platform's data access layer so each service does not
  reimplement per-schema/per-database migration fan-out independently.

## Alternatives considered

- **Single shared schema + `tenant_id` column only** — rejected as insufficient for BAA-bound
  enterprise accounts; acceptable only as the default for self-serve/trial tenants with lighter
  compliance requirements.
- **Fully separate database cluster per tenant** — rejected as the platform-wide default due to
  cost and operational overhead at scale; retained as an available deployment option for
  large hospital systems requiring dedicated infrastructure (see
  [deployment-architecture.md](../deployment/deployment-architecture.md)).
