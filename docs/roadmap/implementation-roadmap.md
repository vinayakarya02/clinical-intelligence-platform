# Implementation Roadmap

**Status:** Phases 0–4 delivered. **Phase 1 document-intelligence pipeline, Phase 2
hybrid retrieval, and Phase 3 clinical copilot are implemented** — see the Phase 1 section below for what
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

## Phase 2 — Retrieval, Knowledge Graph & Conversational AI

### Delivered — retrieval intelligence layer

Implemented, tested, and benchmarked; see
[phase-2-engineering-report.md](../design/phase-2-engineering-report.md) and
[services/retrieval/README.md](../../services/retrieval/README.md).

- [x] Embedding pipeline behind a provider protocol: batching, retry with jittered backoff,
      caching, deduplication, and a `provider/model/dimensions` key carried on every stored
      vector so a model change is a re-index rather than a silent mixing of vector spaces
- [x] Vector store — MongoDB Atlas `$vectorSearch` ([ADR-0007](../design/adr-0007-vector-store-mongodb-atlas.md))
      with tenant and model filters pushed *inside* the index, plus an exact in-memory backend
      with identical filter and threshold semantics for development and CI
- [x] Knowledge graph engine on Neo4j ([ADR-0002](../design/adr-0002-graph-database-choice.md)):
      ontology-aware node labels, provenance enforced at construction for clinically actionable
      relationships, confidence scores, hop-bounded traversal
- [x] Query router (intent → per-strategy weights, concurrent dispatch, widen-when-unsure) and
      weighted Reciprocal Rank Fusion over per-strategy rankings
- [x] Reranking — interpretable seven-feature linear scorer behind the `Reranker` protocol
- [x] Context assembly — token budgeting, content deduplication, citation ordering, attributed
      graph evidence, and a full retrieval trace
- [x] Prompt orchestration — versioned templates in a registry, validated at load
- [x] Evaluation framework — Precision@K, Recall@K, MRR, NDCG, hit rate, context precision/recall,
      citation accuracy, faithfulness, groundedness, numeric consistency, latency percentiles,
      graph coverage

### Remaining

- Entity extraction and resolution from ingested text (the graph is currently populated by
  explicit writes, so its coverage is whatever a separate process wrote)
- Leiden community detection with run-ID-based community stability, and deferred/lazy community
  summarization for global search (opt-in trigger + 24h SLA)
- Conflict resolution (`SUPERSEDES` pattern) for contradictory patient facts
- Neo4j Causal Cluster with read replicas and a Redis hot-traversal cache
- Cross-source consistency checking across fused results
- Cross-encoder reranking (self-hosted) — the feature reranker is the baseline it must beat
- A clinical embedding model to replace the deterministic lexical baseline
- Conversational AI Service: session management with context-window/summarization handling,
  grounding/citation *enforcement* (the metrics exist; nothing blocks on them yet),
  deterministic numeric-value verification, guardrails
- Ontology-registry pattern for adding regional ontologies as data, not code
- A curated, clinician-reviewed eval set — every ranking claim currently rests on 6 cases

**Exit criteria:** multi-hop clinical questions answered with cited, grounded responses;
measurable accuracy improvement over the Phase 1 vector-only baseline on the eval set, including
the numeric-accuracy sub-metric.

## Phase 3 — Clinical Copilot (intelligence layer delivered)

Implemented, tested, and benchmarked; see
[phase-3-engineering-report.md](../design/phase-3-engineering-report.md) and
[services/copilot/README.md](../../services/copilot/README.md).

- [x] Multi-turn conversation: working / episodic / semantic memory, reference resolution,
      clarification when a reference cannot be resolved
- [x] Clinical reasoning over retrieved evidence, the knowledge graph, structured records, and
      tool results, aggregated into one deduplicated, ranked evidence set
- [x] Explainable AI: cited evidence, narrated graph chains, full stage trace, decomposed
      confidence, and a plain-language uncertainty explanation on every answer
- [x] Tool framework: ten clinical tools behind one registry with JSON-Schema argument
      validation, PHI-class scope checks, and approval gating
      ([ADR-0009](../design/adr-0009-deterministic-orchestration.md))
- [x] Agentic workflow as eight independently testable stages over an immutable state
- [x] Reflection as deterministic verification against cited evidence, dropping rather than
      rewriting ([ADR-0010](../design/adr-0010-verification-not-self-critique.md))
- [x] Clinical safety: insufficient evidence, contradiction, staleness, ambiguity, and
      dangerous-combination detectors with severity-driven handling
- [x] Structured responses: Markdown, JSON, compact API envelope, FHIR `DocumentReference` +
      `Provenance` bundle
- [x] Timeline intelligence across encounter / condition / medication / observation tracks
- [x] Human-in-the-loop suspend, resume, and deny
- [x] Prompt registry v2: deployment pins, rollback, session-stable experiments
- [x] Evaluation: planner recall, verification rate, hallucination rate, abstention
      correctness, citation rate, graph utilisation, latency, tokens, cost

### Remaining

- A real language model behind the `LanguageModel` protocol — every quality figure in this
  phase is a property of the deterministic extractive composer
- An EHR/FHIR adapter behind `ClinicalDataSource`; the only implementation is in-memory
- A curated, clinician-reviewed reasoning eval set (six cases today)
- An LLM planner for question shapes outside the rule set
- Streaming responses, and a shared memory store for multi-replica deployment

## Phase 4 — Production Platform (delivered)

Implemented, tested, and benchmarked; see
[phase-4-engineering-report.md](../design/phase-4-engineering-report.md),
[08-production-platform.md](../architecture/08-production-platform.md), and
[libs/cip_platform/README.md](../../libs/cip_platform/README.md).

- [x] Production Dockerfile (multi-stage, non-root, read-only rootfs) with one image and three
      entrypoints ([ADR-0017](../design/adr-0017-worker-topology.md)), plus a development
      compose stack
- [x] 20 Kubernetes objects: Deployments, StatefulSet, Services, Ingress, ConfigMap, Secret
      shape, HPA, PDB, NetworkPolicies, restricted Pod Security Standard, and three distinct
      probe types — validated in CI by a policy script that checks what a schema cannot
- [x] Five-domain cache (embedding, retrieval, session, prompt, graph) with TTLs, namespace
      invalidation, and a tenant in every key ([ADR-0014](../design/adr-0014-cache-topology.md))
- [x] Background workers: six job kinds, classified retries, idempotency, dead-lettering, and
      a scheduler whose window-derived keys make duplicate enqueues deduplicate
- [x] Event spine with correlation and causation ids, W3C trace context, and audit emitted by
      the bus itself ([ADR-0015](../design/adr-0015-event-spine.md))
- [x] AI observability on the OpenTelemetry GenAI semantic conventions, with local extensions
      enumerated ([ADR-0016](../design/adr-0016-otel-genai-conventions.md))
- [x] Monitoring: metric registry with a cardinality guard, Prometheus scrape config, 9 alert
      rules, a 12-panel Grafana dashboard, and a runbook per alert
- [x] Security: hashed API keys with constant-time comparison, RBAC, per-tenant and
      per-principal rate limits, spend budgets ([ADR-0018](../design/adr-0018-cost-governance.md)),
      secret providers, and configuration that refuses unsafe deployments at startup
- [x] MLOps: model, embedding, and evaluation registries with promotion, one-call rollback, and
      a compatibility matrix that refuses an un-evaluated artifact combination
- [x] CI/CD: format, lint, types, unit tests, integration tests against real services,
      architecture validation, security and dependency scanning, image build with SBOM and
      provenance, and manifest policy checks
- [x] Environment-aware configuration for development, testing, staging, and production
- [x] Benchmarks across cache, limits, auth, metrics, events, and the end-to-end copilot

### Remaining

- **Validation against real infrastructure.** Nothing has run against a real Redis, broker,
  Kafka, or Kubernetes cluster; the image has never been built. This is the largest gap in the
  project.
- Kafka and Celery backends (protocols and in-memory implementations exist)
- HTTP routes on the gateway; the middleware is exercised directly, not through a request
- Load testing — every throughput figure is single-process and sequential
- Blue-green and canary automation; the manifests support a rolling update only
- Distributed tracing emission (the OTLP endpoint is configured; no spans are created)

## Phase 5 — Clinical Decision Intelligence (delivered)

Implemented, tested, benchmarked, and evaluated; see
[phase-5-engineering-report.md](../design/phase-5-engineering-report.md),
[09-clinical-decision-intelligence.md](../architecture/09-clinical-decision-intelligence.md),
and [services/decision/README.md](../../services/decision/README.md).

> **The knowledge corpus shipped with this phase has not been clinically reviewed.** The engine
> is production-grade; the content is demonstration data. See the
> [clinical safety case](../safety/clinical-safety-case.md).

- [x] Deterministic decision engine — assemble → evaluate → check → score → rank → detect →
      suppress → explain → gate, with no model in the decision path
      ([ADR-0022](../design/adr-0022-deterministic-decisions.md))
- [x] Rules engine over a typed condition AST with **no `eval`**, and three-valued evaluation in
      which unknown is never read as false
- [x] Knowledge as versioned, cited, dated YAML — rules, guidelines, interactions, dose limits,
      risk models, pathways — with a loader that refuses uncited, misspelled, duplicated, or
      unsupported artifacts ([ADR-0019](../design/adr-0019-knowledge-as-data.md)), enforced by a
      test that fails the build if a drug name appears in engine code
- [x] Drug intelligence: interaction, allergy, duplicate therapy, drug–condition, dose ceiling,
      organ-function, and drug–age checks, with severity and evidence quality kept as
      independent axes ([ADR-0020](../design/adr-0020-severity-and-evidence.md)) and allergy
      cross-reactivity declared per class rather than assumed
- [x] Risk stratification that reports **no score at all** outside a model's population, with
      the unevaluable contribution carried explicitly
- [x] Care pathways shaped after FHIR `PlanDefinition`, applicability evaluated by the same
      rules engine, not-applicable actions retained with their reason
      ([ADR-0023](../design/adr-0023-fhir-clinical-reasoning.md))
- [x] Alert suppression as a designed, measured, audited stage: deduplication by clinical
      concern, per-patient override memory, per-role severity floor, and a volume ceiling —
      with contraindications exempt from all four
      ([ADR-0021](../design/adr-0021-alert-fatigue.md))
- [x] CDS Hooks 2.0 discovery, services, and cards, with `medication-prescribe` implemented
      *and marked deprecated* so an integrator sees it before building against it
- [x] SMART-on-FHIR launch context handling that does not require a live FHIR server
- [x] Event-driven clinical workflows on the Phase 4 event spine, with a notification floor
      distinct from the display floor
- [x] Evidence graph rendering guideline → rule → fact → recommendation as a queryable path
- [x] Human approval gate with no auto-accept path and no flag that disables it
      ([ADR-0024](../design/adr-0024-human-approval-gate.md))
- [x] Evaluation framework reporting recall, false positives against forbidden labels, alert
      burden, and rule coverage side by side and never combined; 5/5 labelled cases at 100%
      rule coverage
- [x] Adversarial review: three Blockers and five High findings, each fixed with a regression
      test; benchmarks and workflow simulations rerun

### Remaining

- **Clinical review of the knowledge corpus.** The largest gap in the phase, and a
  precondition for any use.
- **A licensed drug interaction database.** A hand-maintained list does not scale; the
  maintenance burden is why those products are commercial.
- **Measurement of alert burden with real clinicians.** The suppression defaults come from
  published literature, not from this system's behaviour in a service. Override rate is the
  metric that matters and has never been measured here.
- CQL support, `RequestGroup` cardinality behaviours, and three-drug interactions
- HTTP routes serving CDS Hooks from the gateway; the services are exercised in-process
- Persistence for approval records and the evidence graph — both are in-process and bounded
- A regulatory assessment. Depending on jurisdiction and claims, this may be a medical device.

## Phase 6 — Clinical Ecosystem Interoperability (delivered)

Implemented, tested, benchmarked, and load-simulated; see
[phase-6-engineering-report.md](../design/phase-6-engineering-report.md),
[10-clinical-ecosystem-interoperability.md](../architecture/10-clinical-ecosystem-interoperability.md),
and [services/interop/README.md](../../services/interop/README.md).

> **Nothing in this phase has exchanged a message with a real hospital system.** Every
> conformance claim is against a specification document, not against a counterparty.

- [x] HL7 v2 engine: MLLP framing with a required frame bound, a scanner that reads delimiters
      from `MSH-1`/`MSH-2` rather than assuming them, escape decoding on access, repetitions
      preserved at every level, `Z` segments retained, ADT/ORM/ORU/SIU/DFT profiles, and `AA` /
      `AE` / `AR` kept distinct because senders' retry logic depends on it
      ([ADR-0025](../design/adr-0025-hl7-parsing.md))
- [x] FHIR gateway: 18 resource types as element definitions rather than classes, R4 and R5 from
      one set with version-specific elements declared, validation covering cardinality,
      primitive syntax, required bindings, reference targets, choice exclusivity, and
      unrecognised modifier extensions; versioned storage with weak-ETag optimistic concurrency;
      atomic transaction and independent batch bundles with `urn:uuid:` resolution; a
      `CapabilityStatement` generated from what is registered so it can only understate
- [x] Declarative HL7-to-FHIR mapping refused at load for an unknown transform, a non-existent
      target element, a missing version, or two mappings writing one target
      ([ADR-0026](../design/adr-0026-mapping-as-data.md)), enforced by a test that fails the
      build if an HL7 field path appears in engine code
- [x] EMPI: Fellegi-Sunter with two thresholds and a **review zone**, correlated field groups so
      a shared household is not counted twice, missing fields neutral, national identifiers
      promoting but never demoting, reversible merges, split, and full link history
      ([ADR-0027](../design/adr-0027-empi-review-not-automerge.md))
- [x] Multi-organisation architecture: a validated four-kind hierarchy, directional dated
      purpose-scoped sharing agreements, and a refusal that names which of the four
      preconditions is missing ([ADR-0030](../design/adr-0030-cross-organisation-sharing.md))
- [x] Consent: deny-by-default, evaluated at disclosure with a required purpose,
      `no_consent_on_file` distinct from `denied`, forward-only revocation, and break-glass that
      writes its audit record **before** returning data
      ([ADR-0028](../design/adr-0028-consent-deny-by-default.md))
- [x] Event streaming: partitioned by resolved person, ordered by source sequence rather than
      wall clock, at-least-once with an idempotency ledger in the consumer, deliberate replay,
      and ordering violations reported rather than smoothed over
      ([ADR-0029](../design/adr-0029-event-ordering.md))
- [x] Integration engine: channels with independent destination queues, retries classified
      transient vs permanent, a bounded dead-letter queue that counts what it drops, and
      retransmission suppression on the message control id
- [x] Imaging: DICOM study/series/instance identity with UID validation, PACS retrieval
      endpoints, modality worklist reconciliation with an explicit unreconciled queue, and a
      FHIR `ImagingStudy` projection - **no pixel data is read, stored, or transmitted**
- [x] Population health: cohorts, prevalence with its denominator, risk-band segmentation
      including the rising-risk band, and quality measures keeping exclusions and exceptions
      separate; small cells suppressed
- [x] Data lake: bronze/silver/gold layering, Safe Harbor de-identification with a manifest
      naming the method and ruleset version, a limited data set labelled as requiring a data use
      agreement, and a point-in-time feature store
      ([ADR-0031](../design/adr-0031-deidentification-safe-harbor.md))
- [x] Clinical APIs: one canonical model with four projections and authorisation below them,
      API version in the path and FHIR version by content type, asynchronous bulk export with a
      manifest stating its retention, and per-line bulk import outcomes
      ([ADR-0032](../design/adr-0032-api-surface-and-versioning.md))
- [x] Real-time dashboards: four audience projections over the stream, carrying no patient
      identifiers
- [x] Cross-system workflows: referral, lab order, imaging order, and discharge as table-driven
      `Task` state machines with an explicit terminal set and a per-kind staleness threshold
- [x] Enterprise security: OIDC/OAuth2 claim validation, SMART v2 scopes parsed as a grammar
      with granular constraints applied as filters, patient launch context enforced,
      deny-overrides ABAC, delegation constrained to a subset, and SCIM provisioning that
      deactivates rather than deletes
- [x] Observability: correlation and W3C trace context created at ingest, PHI summarised to keys
      in audit output, and operational metrics for dead letters, ordering violations, consumer
      lag, review queue depth, and break-glass
- [x] Multi-region and disaster recovery design with per-service RPO/RTO and a reconciliation
      procedure ([multi-region-dr.md](../deployment/multi-region-dr.md))
- [x] Adversarial review: three Blockers and four High findings, each fixed with a regression
      test; 162 new tests; benchmarks and a 500-message load simulation

### Remaining

- **No counterparty testing.** Not one message has been exchanged with a real EHR, lab, or PACS.
  This is the largest gap in the phase and no internal testing closes it.
- **Matching probabilities are unestimated.** The shipped `m`/`u` values are defaults and must be
  derived from the deployment's own population.
- **No review queue has ever been worked**, so the design's safety valve is untested in practice.
- No licensed terminology validation (SNOMED CT, LOINC, RxNorm).
- JWT signature verification, which needs a JWKS endpoint.
- Partial FHIR search: no chaining, `_include`, `_has`, or subscriptions - declared absent in the
  `CapabilityStatement`.
- DR is designed and never exercised; no failover has been performed.
- Throughput is single-process and population-dependent.

## Phase 7 — Analytics Warehouse & Self-Service Reporting (delivered)

Implemented, tested, and benchmarked; see
[phase-7-engineering-report.md](../design/phase-7-engineering-report.md),
[11-analytics-warehouse.md](../architecture/11-analytics-warehouse.md), and
[services/analytics/README.md](../../services/analytics/README.md).

- [x] Dimensional warehouse: seven facts over six conformed dimensions, one declared grain per
      fact, declared additivity per measure, and a typed store whose row iterator requires an
      organisation so a cross-tenant scan is unwritable
- [x] De-identifying ETL: watermarked and incremental against the source's own cursor with a
      declared ordering, idempotent via a declared natural key, Safe Harbor applied at load so
      the warehouse holds no direct identifiers and the salt never enters it
      ([ADR-0033](../design/adr-0033-deidentify-at-load.md))
- [x] Semantic layer: 18 metrics declared as versioned data, validated against the schema at
      load, refusing an unknown column, a ratio with no denominator, a missing disclosure policy,
      a duplicate key, or a patient-level metric with no subject column
      ([ADR-0034](../design/adr-0034-metric-is-a-definition.md))
- [x] Template-only query surface: typed parameters with declared bounds, permitted groupings, a
      quasi-identifier budget, and a required scope — no free-form SQL anywhere
      ([ADR-0035](../design/adr-0035-no-free-form-queries.md))
- [x] Statistical disclosure control: primary suppression, complementary suppression so a
      withheld cell cannot be recovered by subtraction, total suppression, and refusal when no
      combination is safe ([ADR-0036](../design/adr-0036-complementary-suppression.md))
- [x] The four Phase 0 dashboard categories — clinical/pharmacovigilance, operational,
      governance, usage — composed from metric keys, with a failed tile rendered as a failed tile
      rather than silently dropped
- [x] Scheduled reports: declared schedules in UTC with catch-up, a declared run-as principal,
      Markdown/CSV/JSON rendering carrying lineage and suppression notes, and delivery of a
      failure notice when a run fails
- [x] `/analytics/*` API matching the Phase 0 OpenAPI declaration, read-only by construction,
      with freshness in a header and refusals that name their cause
- [x] Adversarial review: one Blocker and three High findings, each fixed with a regression test;
      89 new tests; benchmarks

### Remaining

- **The loaders are not wired to the Phase 1-6 stores.** The contract is exercised against
  generated extracts; until they read the live systems the warehouse is empty, which `health()`
  reports as `503 warehouse-empty` rather than answering every question with zero.
- No warehouse product behind the store contract (BigQuery/Synapse/Redshift).
- No scheduler runtime: `due()` decides what should run, and something must call it.
- No analytics UI; the API returns JSON.
- Metric definitions are plausible and unreviewed by a clinical or pharmacovigilance specialist.
- Cell suppression does not defend against differencing across many correlated queries, and
  differential privacy is not claimed.

## Phase 8 — Enterprise Hardening & Compliance Certification

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

## Phase 9 — Scale & Advanced Retrieval (future)

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

## Phase 10+ — Not yet planned (explicitly out of scope, not silently missing)

- Third-party integration / partner marketplace (plugin SDK, external webhook consumers beyond
  the platform's own ingestion pipeline)
- Federated learning across tenants
- Autonomous multi-step clinical decision-making and voice/ambient interfaces (see
  [04-conversational-ai.md §6](../architecture/04-conversational-ai.md#6-not-in-scope-for-phase-01)) —
  any future work here requires its own architecture review given the materially different risk
  profile from the grounded-QA and human-gated decision-support system designed in
  Phases 1-7

## Related documents

- [Phase 0 Architecture Review](../design/phase-0-architecture-review.md)
- [Phase 0 Sign-off](phase0-signoff.md)
- [Non-Functional Requirements](../nfr.md)
