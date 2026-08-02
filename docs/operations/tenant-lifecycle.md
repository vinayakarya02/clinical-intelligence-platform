# Tenant Lifecycle: Onboarding & Offboarding

**Status:** Phase 0 — Design only
**Added:** Phase 0 review found the schema implied a full tenant lifecycle
(`platform.tenants.status` including `offboarded`) with no operational process behind it — see
[phase-0-architecture-review.md](../design/phase-0-architecture-review.md) finding A5.

## 1. Onboarding

1. **Contract & BAA execution** — signed BAA recorded (`tenants.baa_signed_at`), tenant type and
   data-residency requirements captured, driving the deployment topology choice in
   [deployment-architecture.md §1](../deployment/deployment-architecture.md#1-deployment-topologies).
2. **Isolation mode decision** — `schema_per_tenant`, `database_per_tenant`, or `shared_rls`,
   decided per [ADR-0003](../design/adr-0003-multi-tenancy-model.md) based on the tenant's
   contractual isolation requirements and size.
3. **Infrastructure provisioning** — Postgres schema/database, Neo4j database (if
   `database_per_tenant`), object storage prefix + IAM policy, pooler shard assignment (
   [ADR-0003 Consequences](../design/adr-0003-multi-tenancy-model.md#consequences)).
4. **IdP federation** — OIDC/SAML (cloud) or AD/ADFS/LDAP (on-prem, per
   [deployment-architecture.md §1](../deployment/deployment-architecture.md#1-deployment-topologies))
   configured and tested with a tenant admin account.
5. **Ontology licensing check** — confirm the tenant's jurisdiction is covered under the
   platform's existing SNOMED CT/UMLS licensing, or trigger a jurisdiction-specific licensing
   step before go-live (see [docs/legal/ontology-licensing.md](../legal/ontology-licensing.md)).
6. **Initial data load** — first ingestion batch (via `POST /documents/batch` or
   `POST /documents/fhir-bulk-import`, see [openapi.yaml](../api/openapi.yaml)), validated against
   the tenant-specific slice of the eval set before the tenant is marked `active`.
7. **Status transition**: `trial` → `active` once steps 1–6 complete.

## 2. Offboarding

Triggered via `POST /admin/tenants/{tenantId}/offboard` ([openapi.yaml](../api/openapi.yaml)),
which transitions `tenants.status` to `offboarding` (a time-boxed transitional state, not an
immediate deletion):

1. **Data export** — a machine-readable export (FHIR NDJSON for clinical facts, original
   documents from object storage, graph export as a Cypher/CSV bundle) is generated and made
   available to the tenant within a contractually-committed window (target: 30 days from
   offboarding start).
2. **Confirmation window** — tenant confirms export receipt/completeness before deletion proceeds
   (default 15-day window, configurable per contract); this is a deliberate pause, not an
   automatic pipeline straight to deletion.
3. **Verified deletion** — every tenant-scoped row across Postgres (`deleted_at` set, then
   hard-deleted once `purge_after` passes — see
   [postgres-schema.sql](../database/postgres-schema.sql)), Neo4j (tenant database dropped or
   tenant-scoped nodes/edges purged in shared-database mode), OpenSearch (tenant-partitioned
   documents removed), object storage (tenant prefix deleted, including versioned/replicated
   copies), and the analytics warehouse (tenant partition dropped). Encryption-key destruction
   (for any tenant using customer-managed KMS keys, per
   [06-security-compliance.md §9](../architecture/06-security-compliance.md#9-encryption)) is the
   final step, rendering any residual replicated/backup data cryptographically unrecoverable even
   if a physical deletion pass was incomplete.
4. **Deletion certificate** — a signed attestation of deletion completion, including which stores
   were purged and when, delivered to the tenant — this is the artifact a hospital's own
   compliance team needs to close out their side of the BAA termination.
5. **Status transition**: `offboarding` → `offboarded`, with `data_purge_completed_at` set on the
   `tenants` row (itself retained as a minimal record — tenant existed, was offboarded on date X —
   even after all tenant *content* is purged, since the platform's own audit trail requires a
   record that the tenant relationship existed).

## 3. Suspension (distinct from offboarding)

A tenant can be `suspended` (e.g., non-payment, contract dispute) without triggering deletion —
data remains intact and isolated, but API access is blocked at the gateway. Suspension is
reversible; offboarding is not. This distinction exists in `tenants.status` specifically so a
billing dispute doesn't risk accidental data loss.

## 4. Related documents

- [ADR-0003: Multi-tenancy model](../design/adr-0003-multi-tenancy-model.md)
- [SLA, DR, incident response & cost model](sla-dr.md)
- [PostgreSQL schema](../database/postgres-schema.sql)
- [API specification](../api/openapi.yaml)
