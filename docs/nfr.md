# Non-Functional Requirements (Consolidated)

**Status:** Phase 0 — Design only
**Added:** Phase 0 review found latency, throughput, and scale targets scattered across multiple
documents with no single reference — see
[design/phase-0-architecture-review.md](design/phase-0-architecture-review.md) findings A14, A15,
C17, C19. This document consolidates rather than restates — each row links to the section that
owns the underlying design.

## 1. Latency

| Target | Value (p95) | Source |
|---|---|---|
| Retrieval total (fused, reranked, ACL-filtered) | < 320 ms | [02-rag-hybrid-retrieval.md §5](architecture/02-rag-hybrid-retrieval.md#5-latency--cost-budget-design-targets-to-be-validated-in-phase-1) |
| LLM generation, first token (streamed) | < 1.5 s | Same |
| Graph local search (hop-bounded) | < 50 ms | Same |
| Availability | 99.9%–99.95% by tier | [sla-dr.md §1](operations/sla-dr.md#1-service-level-targets-design-targets-to-be-validated-in-phase-1) |

## 2. Scale ceilings

These are the numbers the qualitative "what happens at 10x/100x tenants" discussions in
[ADR-0003](design/adr-0003-multi-tenancy-model.md#consequences) and
[ADR-0002](design/adr-0002-graph-database-choice.md#consequences) point to. They are Phase 0
estimates to plan against, not measured production limits.

| Dimension | Estimated ceiling at current design | First bottleneck | Mitigation |
|---|---|---|---|
| Tenants per Postgres cluster (schema-per-tenant) | Low hundreds before pooler-shard count and per-schema migration fan-out time become operationally heavy | Migration fan-out time growing linearly with schema count | Tenant-sharded pooler tier ([ADR-0003](design/adr-0003-multi-tenancy-model.md#consequences)); parallelized migration runner (Phase 1 tooling) |
| Tenants per Neo4j deployment (database-per-tenant) | Bounded by Aura/self-hosted cluster's practical databases-per-instance limit — believed to be the platform's first hard ceiling, likely reached before the Postgres ceiling above | Databases-per-instance limit | Horizontal sharding of tenants across multiple Neo4j clusters, routed at the Retrieval Service layer (a routing change, not a re-architecture) |
| Patients per tenant (graph node/edge count) | Tens of millions of patients, hundreds of millions of edges is the target upper bound for a single large hospital-system tenant | Local-search traversal latency without caching | Redis hot-traversal cache + Neo4j read replicas ([deployment-architecture.md §2](deployment/deployment-architecture.md#2-reference-cloud-architecture-multi-tenant-topology)) |
| `audit_log` rows | Multi-billion within 1-2 years for one active hospital tenant logging every request | Unpartitioned table degrading autovacuum/index maintenance | Monthly range partitioning + hot/cold tiering ([postgres-schema.sql](database/postgres-schema.sql), [sla-dr.md §3](operations/sla-dr.md#3-audit-log-integrity--retention)) |

**Revisit trigger**: when Phase 1 telemetry gives real per-tenant connection counts, real graph
node/edge growth rates, and real Neo4j Aura instance limits, replace the estimates above with
measured numbers and update the mitigation triggers accordingly — this table is deliberately
conservative and will be wrong in the specifics; its purpose is to make sure scale is planned
against *something* rather than nothing.

## 3. Throughput (design targets)

| Target | Value | Source |
|---|---|---|
| Bulk ingestion batch size | Up to 5,000 documents per `POST /documents/batch` call | [openapi.yaml](api/openapi.yaml) |
| Eval-set size at Phase 1 launch | ≥ 150 Q&A pairs per tenant vertical | [02-rag-hybrid-retrieval.md §4](architecture/02-rag-hybrid-retrieval.md#4-evaluation--observability) |
| Eval-set size at Phase 2 | ≥ 500 Q&A pairs per tenant vertical | Same |
| Community summarization first-run SLA | Within 24h of tenant opt-in | [03-knowledge-graph.md §5](architecture/03-knowledge-graph.md#5-community-summarization-opt-in-trigger--sla) |

## 4. Related documents

- [Glossary](glossary.md)
- [SLA, DR, Incident Response & Cost Model](operations/sla-dr.md)
- [Deployment Architecture](deployment/deployment-architecture.md)
- [Phase 0 Architecture Review](design/phase-0-architecture-review.md)
