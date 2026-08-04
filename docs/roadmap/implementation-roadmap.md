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

## Phase 6 — Analytics & Dashboards

- Analytics warehouse ETL (de-identified aggregate pipeline)
- Dashboard categories: clinical/pharmacovigilance, operational, governance, usage
  (per [05-analytics-dashboard.md](../architecture/05-analytics-dashboard.md))
- Scheduled report generation

**Exit criteria:** tenant admins and analysts have self-service dashboards without needing
direct database access.

## Phase 7 — Enterprise Hardening & Compliance Certification

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

## Phase 8 — Scale & Advanced Retrieval (future)

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

## Phase 9+ — Not yet planned (explicitly out of scope, not silently missing)

- Third-party integration / partner marketplace (plugin SDK, external webhook consumers beyond
  the platform's own ingestion pipeline)
- Federated learning across tenants
- Autonomous multi-step clinical decision-making and voice/ambient interfaces (see
  [04-conversational-ai.md §6](../architecture/04-conversational-ai.md#6-not-in-scope-for-phase-01)) —
  any future work here requires its own architecture review given the materially different risk
  profile from the grounded-QA and human-gated decision-support system designed in
  Phases 1-5

## Related documents

- [Phase 0 Architecture Review](../design/phase-0-architecture-review.md)
- [Phase 0 Sign-off](phase0-signoff.md)
- [Non-Functional Requirements](../nfr.md)
