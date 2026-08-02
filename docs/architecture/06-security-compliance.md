# Security, Multi-Tenancy & Compliance

**Status:** Phase 0 — Design only
**Depends on:** [ADR-0003](../design/adr-0003-multi-tenancy-model.md)
**Revised after Phase 0 review:** see [phase-0-architecture-review.md](../design/phase-0-architecture-review.md) findings B1, D2, D8, A2, A4.

This document is the compliance baseline referenced throughout the architecture. It is a design
input to every other document, not an afterthought layered on at the end.

## 1. Regulatory scope

| Framework | Applies to | Key requirements this architecture must satisfy |
|---|---|---|
| **HIPAA** | All hospital/clinical-data tenants (US) | De-identification (Safe Harbor or Expert Determination), minimum-necessary access, audit logging (45 CFR §164.312(b)), Business Associate Agreements with every subprocessor touching PHI (including LLM API vendors) |
| **HITRUST CSF** | Enterprise procurement requirement for most hospital-system customers | Third-party security assurance; increasingly maps to 21 CFR Part 11 |
| **SOC 2 Type II** | Standard cloud vendor trust requirement | Requested alongside HITRUST in most enterprise deals |
| **21 CFR Part 11** | Pharma tenants using the platform for regulated clinical-trial data | Electronic records/signatures validation; relevant to FDA-regulated data flows only |

## 2. Multi-tenancy model

Full design and rationale: [ADR-0003](../design/adr-0003-multi-tenancy-model.md). Summary:
defense-in-depth isolation — schema/database-per-tenant at the storage layer for regulated
accounts, Row-Level Security as the enforced floor everywhere else, and a mandatory
`(tenant_id, actor_scopes)` context on every cross-store query with no code path exempted.

## 3. Identity, authentication & authorization

- **AuthN**: OIDC/SAML federation with each tenant's enterprise IdP (Okta, Azure AD, etc.) —
  the platform does not become a primary credential store for enterprise tenants.
- **AuthZ**: RBAC for coarse roles (`admin`, `clinician`, `analyst`, `pharmacovigilance_reviewer`,
  `viewer`) layered with ABAC-style scoped policies for minimum-necessary enforcement (e.g., a
  role can be scoped to a specific department, patient panel, or de-identified-only view).
- **Tokens**: short-lived, scope-bound tokens issued by the Identity & Access Service; every
  downstream service performs token introspection rather than trusting a passed-through identity
  claim. A token's tenant claim and the request's tenant context (URL subdomain in the multi-tenant
  API — see [openapi.yaml](../api/openapi.yaml)) must match exactly; a mismatch is a hard `403`,
  a validated invariant rather than an assumed one
  ([review finding D19](../design/phase-0-architecture-review.md)).
- **Emergency ("break-glass") access**: a dedicated role (`emergency_access`) and endpoint grant a
  clinician short-lived, out-of-normal-panel access to a patient record in an emergency. Every
  break-glass grant is logged to `audit_log` with `action = 'access.break_glass'` and
  `phi_accessed = true` at issuance (not just at first use), triggers a notification to the
  tenant's compliance contact, and expires automatically within a fixed short window (default 4
  hours). This is a standard clinical-system requirement that had no home in the original design
  ([review finding D8](../design/phase-0-architecture-review.md)) — see
  `POST /access/break-glass` in [openapi.yaml](../api/openapi.yaml).

## 4. De-identification

Two supported modes, configurable per tenant/use case:

- **Safe Harbor** — removal of the 18 HIPAA-defined identifier categories; default for analytics
  warehouse ETL ([05-analytics-dashboard.md](05-analytics-dashboard.md)).
- **Expert Determination** — statistical re-identification risk assessment with documented
  methodology; used when a tenant needs to retain dates/geography/longitudinal linkage for
  research or pharmacovigilance analytics that Safe Harbor would strip.

De-identification status is tracked as chunk/entity-level metadata (see
[postgres-schema.sql](../database/postgres-schema.sql) `documents.deidentification_status`) so
retrieval and generation can enforce that a given user's scope only ever sees data at or below
their permitted identification level.

## 5. Audit logging

Every retrieval query, every LLM generation call (including the exact context provided), every
data access, and every administrative action is logged immutably with actor identity, timestamp,
data accessed, and action taken — satisfying 45 CFR §164.312(b). Audit logs are themselves
tenant-isolated and retained per the tenant's contractual retention policy (default 6 years,
matching HIPAA's documentation retention requirement). See
[postgres-schema.sql](../database/postgres-schema.sql) `audit_log` table and
[deployment-architecture.md §5](../deployment/deployment-architecture.md) for log storage/retention
infrastructure.

## 6. LLM vendor governance

**Any external API call that receives clinical text or PHI — generation, extraction, embedding,
reranking, or classification — must be covered by a signed BAA before that call is made.** An
earlier draft of this section only named "generation or extraction," which left the Embedding
Service's calls to third-party embedding providers (candidates evaluated in
[02-rag-hybrid-retrieval.md §1.3](02-rag-hybrid-retrieval.md#13-embedding-model)) outside the
letter of the BAA-gating rule even though those calls receive the same raw clinical chunk text —
corrected per [review finding B1](../design/phase-0-architecture-review.md). The reranker is
self-hosted specifically to avoid re-opening this question on every retrieval query (see
[02-rag-hybrid-retrieval.md §2.2](02-rag-hybrid-retrieval.md#22-fusion-cross-source-consistency-and-reranking)).

The platform maintains a per-tenant configurable allowlist of approved model providers for every
such call type, with an on-prem/open-weight model option for tenants whose data-residency
requirements prohibit any external API call (relevant primarily to hospital systems with
air-gapped or sovereign-cloud requirements). A written inventory of every AI system touching ePHI
is maintained, anticipating the proposed HIPAA Security Rule requirement for such an inventory.

## 7. Data residency & deployment options

Cloud-only, hybrid, and on-prem deployment topologies are all supported — see
[deployment-architecture.md](../deployment/deployment-architecture.md) — because hospital
customers frequently require in-region or on-prem data residency that a cloud-only architecture
cannot satisfy, while pharma/analytics-only customers are typically comfortable with cloud-only.

## 8. Ontology licensing

SNOMED CT, UMLS, and RxNorm are **not unrestricted-use** — SNOMED CT requires an Affiliate
License (free within UMLS-member countries via NLM, but requiring separate national licensing
elsewhere with redistribution restrictions), and the UMLS Metathesaurus itself requires a signed
license with usage/redistribution constraints. This was previously unaddressed anywhere in the
document set ([review finding A2](../design/phase-0-architecture-review.md)) and is a legal/
procurement dependency, not an engineering detail — full treatment in
[docs/legal/ontology-licensing.md](../legal/ontology-licensing.md), tracked as a named Phase 1
prerequisite in [implementation-roadmap.md](../roadmap/implementation-roadmap.md).

## 9. Encryption

Neither this document nor the schema originally specified encryption requirements beyond loading
`pgcrypto` for UUID generation — corrected per
[review finding D2](../design/phase-0-architecture-review.md):

- **In transit**: every database connection requires `sslmode=verify-full`; enforced at the
  connection pooler/proxy layer, not left to client configuration.
- **At rest**: cloud-provider disk/volume encryption with KMS-managed keys (customer-managed keys
  available per tenant for large/regulated accounts) across Postgres, Neo4j, OpenSearch, object
  storage, and the analytics warehouse.
- **Column-level**: specific PHI-bearing free-text fields most likely to appear in an unfiltered
  `SELECT *` during debugging — `patients.mrn`, `document_chunks.chunk_text`,
  `chat_messages.content` — carry a second layer of application-managed envelope encryption
  beyond disk/TLS encryption, per the column comments in
  [postgres-schema.sql](../database/postgres-schema.sql). Key rotation for this layer is a
  platform-KMS integration, tracked as a Phase 1 implementation task.
- **Audit-log tamper-evidence**: `audit_log` rows are hash-chained (`prev_hash`/`row_hash`), so
  even the migration role capable of bypassing RLS cannot retroactively alter history without the
  chain detecting it — see [postgres-schema.sql](../database/postgres-schema.sql) and
  [docs/operations/sla-dr.md §3](../operations/sla-dr.md#3-audit-log-integrity--retention).

## 10. Related documents

- [ADR-0003: Multi-tenancy model](../design/adr-0003-multi-tenancy-model.md)
- [Database schema — audit_log, tenants, RLS policies](../database/postgres-schema.sql)
- [Deployment architecture — compliance-relevant infrastructure](../deployment/deployment-architecture.md)
- [Ontology licensing](../legal/ontology-licensing.md)
- [SLA, DR, incident response & cost model](../operations/sla-dr.md)
- [Tenant lifecycle (onboarding/offboarding)](../operations/tenant-lifecycle.md)
- [Phase 0 Architecture Review](../design/phase-0-architecture-review.md)
