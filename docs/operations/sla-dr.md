# SLA, Disaster Recovery, Incident Response & Cost Model

**Status:** Phase 0 — Design only
**Added:** Phase 0 review found this entirely absent from the original document set — see [phase-0-architecture-review.md](../design/phase-0-architecture-review.md) findings A1, A3, A4, D11, D15.

## 1. Service-level targets (design targets, to be validated in Phase 1)

| Tier | Availability target | Notes |
|---|---|---|
| Cloud-only multi-tenant | 99.9% monthly | Standard multi-tenant SaaS tier |
| Cloud-only dedicated tenant | 99.95% monthly | Dedicated infra reduces noisy-neighbor risk |
| Hybrid/on-prem | Per tenant's own infrastructure SLA for on-prem components; 99.9% for the cloud control plane | The platform cannot commit to an SLA for infrastructure it doesn't operate |

These are targets carried into Phase 4 ("Enterprise Hardening & Compliance Certification" in
[implementation-roadmap.md](../roadmap/implementation-roadmap.md)) for load-testing validation,
not yet measured against a running system.

## 2. Backup & disaster recovery (RPO/RTO)

| Store | RPO target | RTO target | Mechanism |
|---|---|---|---|
| PostgreSQL (operational store) | 5 minutes | 1 hour | Continuous WAL archiving / point-in-time recovery, cross-region read replica promotable on regional outage |
| Neo4j (graph store) | 15 minutes | 2 hours | Causal Cluster read replica promotion (see [deployment-architecture.md §2](../deployment/deployment-architecture.md#2-reference-cloud-architecture-multi-tenant-topology)); full backup snapshot every 6 hours |
| OpenSearch | 1 hour | 2 hours | Rebuildable from Postgres/object storage source of truth if lost — not itself the durability boundary |
| Object storage (raw documents) | Near-zero (versioned, cross-region replicated) | N/A — durable by construction | Cloud-provider versioned bucket replication |
| Analytics warehouse | 24 hours | 4 hours | Rebuildable from operational-store ETL; not a primary durability boundary |

Cross-region failover is a documented runbook (owned by the platform SRE function once Phase 1
infrastructure exists), tested at least twice yearly via a game-day exercise; results feed back
into these targets rather than the targets being asserted once and left unvalidated.

## 3. Audit log integrity & retention

Audit log durability is intentionally decoupled from the operational database's own backup
policy (see [deployment-architecture.md §5](../deployment/deployment-architecture.md#5-observability--audit-infrastructure)):

- **Hot tier**: last 90 days, in `platform.audit_log`'s monthly partitions (see
  [postgres-schema.sql](../database/postgres-schema.sql)), queryable via the Admin API in
  near-real-time.
- **Cold tier**: partitions older than 90 days are moved to WORM (write-once-read-many) object
  storage with object-lock, retaining the hash-chain (`prev_hash`/`row_hash`) verification
  capability, queryable via a federated read path through the Admin API rather than requiring
  direct object-storage access.
- **Retention**: 6 years default (matching HIPAA documentation-retention requirements),
  configurable longer per tenant contract; retention is enforced by policy on the cold tier, not
  by manual deletion.

## 4. Incident response & breach notification

Previously absent entirely — a real production readiness gap for a platform handling PHI.

- **Severity taxonomy**: SEV1 (confirmed or suspected cross-tenant PHI exposure, platform-wide
  outage), SEV2 (single-tenant data-access anomaly, degraded service), SEV3 (non-PHI-impacting
  bug/performance issue).
- **On-call**: 24/7 paging for SEV1/SEV2, standard business-hours triage for SEV3.
- **Breach notification**: any SEV1 involving confirmed PHI exposure triggers the HIPAA breach-
  notification workflow — affected tenant(s) notified within a contractually-committed window
  (target: 72 hours for initial notification, well inside HIPAA's 60-day regulatory maximum per
  45 CFR §164.404), followed by the tenant's own downstream patient-notification obligations,
  which the platform supports with an audit-log-derived scope-of-exposure report rather than
  leaving the tenant to reconstruct it manually.
- **Containment**: the trusted-computing-base framing in
  [01-system-architecture.md §2](../architecture/01-system-architecture.md#2-design-principles)
  (Retrieval Service as the single access-control boundary for downstream consumers) defines the
  first containment target for any suspected cross-tenant leak — isolate/patch there first, then
  verify no other store-level filter was independently compromised.

## 5. Tenant onboarding/offboarding pointer

Operational process (not just the schema's `status` enum) is specified in
[docs/operations/tenant-lifecycle.md](tenant-lifecycle.md).

## 6. Cost model

Previously the only cost treatment in the document set was the infra latency/cost-control section
in [02-rag-hybrid-retrieval.md §5](../architecture/02-rag-hybrid-retrieval.md#5-latency--cost-budget-design-targets-to-be-validated-in-phase-1),
which covers infra spend only. A per-tenant unit-economics model needs four components, tracked
here as line items rather than left unmodeled:

1. **Infra cost** — compute/storage/network per tenant tier (cloud-only shared, cloud-only
   dedicated, hybrid/on-prem), derived from the topology in
   [deployment-architecture.md](../deployment/deployment-architecture.md).
2. **LLM/embedding token spend** — per-query generation cost + per-document embedding cost,
   scaling with query volume and corpus size; tiered model routing (
   [02-rag-hybrid-retrieval.md §5](../architecture/02-rag-hybrid-retrieval.md#5-latency--cost-budget-design-targets-to-be-validated-in-phase-1))
   is the primary lever here.
3. **Ontology licensing fees** — SNOMED CT/UMLS/RxNorm licensing is not free in every
   jurisdiction; see [docs/legal/ontology-licensing.md](../legal/ontology-licensing.md). Amortized
   per-tenant, not a flat platform cost, since licensing terms vary by tenant's country.
4. **Support/compliance overhead** — HITRUST/SOC 2 audit cost, dedicated compliance contact time
   for regulated tenants, break-glass-access review overhead.

This is a Phase 4 pricing-model deliverable (see
[implementation-roadmap.md](../roadmap/implementation-roadmap.md)), listed here so the components
are named now rather than discovered during a sales cycle.

## 7. Related documents

- [Deployment Architecture](../deployment/deployment-architecture.md)
- [Security, Multi-Tenancy & Compliance](../architecture/06-security-compliance.md)
- [Tenant Lifecycle](tenant-lifecycle.md)
- [Ontology Licensing](../legal/ontology-licensing.md)
- [Phase 0 Architecture Review](../design/phase-0-architecture-review.md)
