# Implementation Roadmap

**Status:** Phase 0 complete (architecture, adversarial review, remediation). **Phase 1
document-intelligence pipeline implemented** — see the Phase 1 section below for what
shipped, what was deliberately deferred, and what remains before the phase can be called
complete.
**Revised after Phase 0 review:** see [phase-0-architecture-review.md](../design/phase-0-architecture-review.md).

## Phase 0 — Architecture & Design (this deliverable)

- [x] Research: production RAG, Microsoft GraphRAG, healthcare knowledge graphs, enterprise
      document intelligence architectures, LangChain/LlamaIndex/graph retrieval patterns
- [x] System architecture (context + container diagrams, service inventory)
- [x] RAG & hybrid retrieval design
- [x] Knowledge graph design (entity/relationship schema, GraphRAG pipeline)
- [x] Conversational AI design
- [x] Analytics/dashboard design
- [x] Multi-tenancy, security & HIPAA compliance design
- [x] PostgreSQL schema (DDL)
- [x] Neo4j graph schema
- [x] API specification (OpenAPI)
- [x] Deployment architecture
- [x] Architecture Decision Records (ADR-0001..0004)
- [x] SLA/DR, incident response, and cost-model baseline ([sla-dr.md](../operations/sla-dr.md))
- [x] Tenant lifecycle runbook ([tenant-lifecycle.md](../operations/tenant-lifecycle.md))
- [x] Ontology licensing review ([ontology-licensing.md](../legal/ontology-licensing.md))
- [x] Glossary and consolidated NFRs ([glossary.md](../glossary.md), [nfr.md](../nfr.md))
- [x] Independent adversarial design review — 4 reviewers across architecture/repo/docs, RAG,
      knowledge graph, and database/API dimensions, 74 findings, all Blocker/High findings
      resolved ([phase-0-architecture-review.md](../design/phase-0-architecture-review.md))

**Exit criteria met:** design is internally consistent (every architectural claim in
`docs/architecture/*` traces to a schema, API, or ADR artifact, and cross-document contradictions
found in review — e.g. ADR-0002 vs. deployment-architecture.md on Neptune, ADR-0002 vs.
graph-schema.md on vector index scope — are reconciled), reviewable by engineering leadership and
security/compliance stakeholders without any code having been written, and has survived an
adversarial review specifically tasked with finding gaps rather than validating the design. Formal
sign-off record: [phase0-signoff.md](phase0-signoff.md).

**Named procurement/legal dependencies surfaced by Phase 0** (not engineering tasks, but real
blockers if not started early): SNOMED CT/UMLS licensing for non-US tenants
([ontology-licensing.md](../legal/ontology-licensing.md)); third-party security review vendor
selection ahead of the Phase 1 exit criterion below.

## Phase 1 — Foundational Platform (document intelligence delivered)

Scope: identity/tenancy, ingestion pipeline, operational store, and a single retrieval mode
(vector + keyword hybrid, no graph yet) — the minimum slice that proves the multi-tenant,
compliant data path end-to-end before adding the graph layer's complexity.

Phase 1 was implemented in two tranches. The **document-intelligence pipeline** — everything
from an uploaded document to persisted, quality-gated chunks — is complete. The **retrieval
half** (embeddings, vector index, hybrid search) is not, and the phase's exit criteria are
therefore not yet met.

### Delivered

- [x] Configuration, structured logging with PHI redaction, error taxonomy (RFC 7807), and
      tenant/actor context — `libs/cip_core`
- [x] Multi-tenant PostgreSQL schema and migration, with RLS `FORCE` + `WITH CHECK` policies
      per [ADR-0003](../design/adr-0003-multi-tenancy-model.md)
- [x] MongoDB parsed-artifact store and Neo4j connectivity/health (graph logic is Phase 2) —
      [ADR-0005](../design/adr-0005-phase1-service-decomposition.md)
- [x] Object-storage abstraction (local + S3), tenant-prefixed and content-addressed
- [x] Ingestion pipeline: validation with magic-byte sniffing, content-hash duplicate
      detection, PDF/DOCX/text parsing, per-page OCR fallback, clinical normalisation,
      section detection, metadata extraction, chunking, data-quality gating, persistence
- [x] Audit & Compliance: hash-chained tamper-evident audit log with verification
- [x] Authentication skeleton: token verification and tenant-context derivation
- [x] Document API (upload, list, detail, soft delete) and health endpoints
- [x] CLI: batch ingest, health, config, migrations
- [x] Test suite: 373 tests covering pure logic, persistence against real SQL, and the HTTP
      API; integration tests for RLS/JSONB gated on a live PostgreSQL

### Deliberately deferred within Phase 1

- **Ontology coding** (SNOMED/ICD/LOINC/RxNorm via UMLS) — blocked on licensed terminology
  data ([ontology-licensing.md](../legal/ontology-licensing.md)). The `LocalConcept`
  unmapped-entity fallback is designed and will be populated when coding lands.
  See [ADR-0005](../design/adr-0005-phase1-service-decomposition.md).
- **Embedding-similarity chunking** — requires embeddings; the structural chunker ships
  behind the interface the Phase 2 chunker implements
  ([ADR-0006](../design/adr-0006-phase1-chunking-strategy.md)).

### Remaining before Phase 1 exit

- [ ] Embedding Service + pgvector (per-dimension tables) + OpenSearch hybrid search;
      embedding API calls covered under the BAA-gating policy from day one
- [ ] FHIR R4 + HL7v2 ingestion paths, bulk batch and FHIR Bulk Data ($export/$import) endpoints
- [ ] Ontology coding (see deferral above — procurement-gated, not engineering-gated)
- [ ] Identity & Access Service: OIDC/SAML federation, token issuance, break-glass grants
- [ ] De-identification pipeline (Safe Harbor) and column-level PHI encryption
- [ ] Tenant-sharded connection pooler tier
- [ ] Baseline offline eval set (RAGAS-style metrics, ≥150 SME-authored Q&A pairs per vertical)
- [ ] Tenant onboarding runbook tooling ([tenant-lifecycle.md](../operations/tenant-lifecycle.md))

**Exit criteria:** a tenant can ingest documents and FHIR feeds, and query them via hybrid
vector+keyword retrieval with correct tenant isolation and full audit logging — validated
against the eval set and a named third-party security review (vendor selected and engaged during
Phase 0/1 transition, not left unnamed at exit-criteria time; review cadence continues quarterly
into Phase 2+, with a full HITRUST-track assessor engagement beginning in Phase 4).

## Phase 2 — Knowledge Graph & Conversational AI

- Knowledge Graph Service: entity resolution, graph construction, Leiden community detection with
  run-ID-based community stability, conflict resolution (`SUPERSEDES` pattern) for contradictory
  patient facts
- Neo4j deployment (per [ADR-0002](../design/adr-0002-graph-database-choice.md)), Causal Cluster
  with read replicas, Redis hot-traversal cache, hop-bounded local search
- Query router (structured/vector/keyword/graph path selection, concurrent dispatch) and
  Reciprocal Rank Fusion with cross-source consistency checking
- Cross-encoder reranking (self-hosted)
- Conversational AI Service: session management with context-window/summarization handling,
  grounding/citation enforcement, deterministic numeric-value verification, guardrails
- Deferred/lazy community summarization for global search, opt-in trigger + 24h SLA
- Ontology-registry pattern for adding regional ontologies as data, not code

**Exit criteria:** multi-hop clinical questions answered with cited, grounded responses;
measurable accuracy improvement over the Phase 1 vector-only baseline on the eval set, including
the numeric-accuracy sub-metric.

## Phase 3 — Analytics & Dashboards

- Analytics warehouse ETL (de-identified aggregate pipeline)
- Dashboard categories: clinical/pharmacovigilance, operational, governance, usage
  (per [05-analytics-dashboard.md](../architecture/05-analytics-dashboard.md))
- Scheduled report generation

**Exit criteria:** tenant admins and analysts have self-service dashboards without needing
direct database access.

## Phase 4 — Enterprise Hardening & Compliance Certification

- HITRUST CSF and SOC 2 Type II audits (assessor engaged; cadence and scope tracked alongside the
  Phase 1 third-party security review, not as a newly-introduced process)
- 21 CFR Part 11 validation path for pharma tenants using trial data
- On-prem/hybrid deployment packaging for data-residency-constrained hospital tenants, including
  on-prem AD/ADFS/LDAP identity federation
- Full DRIFT-style blended local/global search, expanded GraphRAG variants as needed
- Load/latency validation against the budgets in [nfr.md](../nfr.md), replacing the Phase 0
  scale-ceiling estimates with measured numbers
- Cost model finalized into tenant pricing (infra + LLM/embedding spend + ontology licensing +
  support overhead — see [sla-dr.md §6](../operations/sla-dr.md#6-cost-model))
- Tenant offboarding runbook exercised end-to-end at least once before the first real offboarding
  event, not tested for the first time under contractual time pressure

**Exit criteria:** platform is sellable into enterprise hospital/pharma procurement processes
that require third-party compliance attestation, with a validated cost model and a demonstrated
(not just designed) offboarding process.

## Phase 5 — Scale & Advanced Retrieval (future)

- Multi-region deployment
- Streaming (near-real-time) analytics warehouse refresh
- Additional graph variants (LightRAG-style incremental updates, TigerVector evaluation per
  [ADR-0002](../design/adr-0002-graph-database-choice.md) revisit trigger) if a tenant's scale
  demands it
- Horizontal sharding of tenants across multiple Neo4j clusters if the scale ceiling in
  [nfr.md §2](../nfr.md#2-scale-ceilings) is reached
- **Imaging-pixel and genomics modalities** (beyond DICOM metadata) — explicitly named here as a
  forward-looking, not-yet-designed extension point, rather than a silent gap (see
  [04-conversational-ai.md §6](../architecture/04-conversational-ai.md#6-not-in-scope-for-phase-01))
- **MLOps/fine-tuning roadmap** — tenant-specific embedding/extraction model adaptation as the
  platform accumulates tenant-specific clinical vocabulary; not yet designed, named so it isn't
  discovered as a gap later

## Phase 6+ — Not yet planned (explicitly out of scope, not silently missing)

- Third-party integration / partner marketplace (plugin SDK, external webhook consumers beyond
  the platform's own ingestion pipeline)
- Federated learning across tenants
- Autonomous multi-step clinical decision-making and voice/ambient interfaces (see
  [04-conversational-ai.md §6](../architecture/04-conversational-ai.md#6-not-in-scope-for-phase-01)) —
  any future work here requires its own architecture review given the materially different risk
  profile from the grounded-QA system designed in Phases 1-4

## Related documents

- [Phase 0 Architecture Review](../design/phase-0-architecture-review.md)
- [Phase 0 Sign-off](phase0-signoff.md)
- [Non-Functional Requirements](../nfr.md)
