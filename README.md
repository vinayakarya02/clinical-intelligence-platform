# Clinical Intelligence Platform (CIP)

An enterprise-grade Clinical Intelligence & Analytics Platform for hospitals, pharmaceutical
companies, and healthcare analytics organizations — combining a clinical knowledge graph, hybrid
(vector + keyword + graph) retrieval-augmented generation, grounded conversational AI, and an
analytics/BI layer over the same governed, access-controlled data substrate.

This is not a chatbot demo and not a simple single-store RAG pipeline. It is designed as a
multi-tenant system with defense-in-depth data isolation, HIPAA-aligned compliance controls, and
provenance/citation on every generated answer — see [docs/architecture/06-security-compliance.md](docs/architecture/06-security-compliance.md).

## Status: Phase 8 — Nine services integrated into one running platform

Phase 0 (architecture and design) is complete and was put through an adversarial,
principal-engineer-level production design review — 4 independent reviewers, 74 findings, all
Blocker and High findings resolved. See
[docs/design/phase-0-architecture-review.md](docs/design/phase-0-architecture-review.md) for the
findings log and [docs/roadmap/phase0-signoff.md](docs/roadmap/phase0-signoff.md) for the
sign-off record.

**Phase 1's document-intelligence pipeline is implemented and tested**: ingestion, validation,
duplicate detection, PDF/DOCX/text parsing with OCR fallback, clinical normalisation, section
detection, metadata extraction, chunking, data-quality gating, and multi-tenant persistence,
behind a FastAPI service and a CLI. Start at
[services/ingestion/README.md](services/ingestion/README.md).

**Phase 2's retrieval intelligence layer is implemented and tested**: an embedding pipeline
with model versioning and caching, a vector store (MongoDB Atlas plus an exact in-memory
backend), a clinical knowledge graph with provenance enforcement and traversal, three
retrievers fused by Reciprocal Rank Fusion under intent-based routing, feature reranking,
token-budgeted cited context assembly, a versioned prompt registry, and an evaluation
harness. Start at [services/retrieval/README.md](services/retrieval/README.md); the
[Phase 2 engineering report](docs/design/phase-2-engineering-report.md) records the
benchmarks, the bugs the end-to-end run found, and an honest production-readiness
assessment.

**Phase 3's clinical copilot is implemented and tested**: multi-turn conversational memory,
a deterministic planner, a ten-tool calling framework with PHI-scoped authorisation, evidence
aggregation, claim construction, verification-based reflection, five clinical safety
detectors, explanation assembly, human-in-the-loop approval, a prompt registry with rollback
and experiments, and Markdown/JSON/API/FHIR output. Start at
[services/copilot/README.md](services/copilot/README.md); the
[Phase 3 engineering report](docs/design/phase-3-engineering-report.md) records the
benchmarks, the eleven defects end-to-end verification found, and an honest readiness
assessment.

**Phase 4's production platform is implemented and tested**: production Dockerfiles and
Kubernetes manifests, a five-domain Redis cache with tenant-scoped keys, an event spine that
emits its own audit records, background workers with classified retries and dead-lettering, AI
observability on the OpenTelemetry GenAI semantic conventions, Prometheus alerts with a runbook
per alert, API keys and RBAC and rate limits and spend budgets, model/evaluation registries
with a compatibility matrix, and an enterprise CI pipeline. Start at
[libs/cip_platform/README.md](libs/cip_platform/README.md); the
[Phase 4 engineering report](docs/design/phase-4-engineering-report.md) records the
benchmarks, the defects the adversarial pass found, and an honest readiness assessment.

**Phase 5's clinical decision intelligence is implemented and tested**: a deterministic
decision engine, a rules engine over a typed expression language with no `eval`, a versioned
cited knowledge base in which no clinical logic lives in code, drug intelligence across seven
checks, risk stratification that refuses to score outside a model's population, FHIR-shaped
care pathways, a role-aware alert-suppression layer built around published override rates,
CDS Hooks 2.0 services and cards, SMART-on-FHIR launch handling, event-driven clinical
workflows, an evidence graph, an approval gate no recommendation can bypass, and an evaluation
framework that reports accuracy and alert burden side by side. Start at
[services/decision/README.md](services/decision/README.md); the
[Phase 5 engineering report](docs/design/phase-5-engineering-report.md) records the benchmarks,
the three Blockers the end-to-end run found, and an honest readiness assessment.

**The clinical knowledge corpus shipped with Phase 5 has not been reviewed by a clinician and
must not be used in the care of real patients.** The engine is production-grade; the content is
demonstration data that looks authoritative and is not. See the
[clinical safety case](docs/safety/clinical-safety-case.md).

**Phase 6's clinical ecosystem interoperability is implemented and tested**: an HL7 v2 engine
with MLLP framing and a parser that reads delimiters from the message rather than assuming them,
a FHIR gateway over 18 resource types serving R4 and R5 from one declarative definition set,
HL7-to-FHIR mapping as versioned data rather than code, an Enterprise Master Patient Index with
a human review zone and reversible merges, a four-level organisation hierarchy with dated
purpose-scoped sharing agreements, a deny-by-default consent engine whose break-glass path
audits before it returns data, a clinical event stream partitioned by resolved person with
idempotent consumers and replay, an integration engine with classified retries and
dead-lettering, DICOM identity and worklist reconciliation, population analytics with quality
measures, Safe Harbor de-identification and a point-in-time feature store, four-audience
dashboards, closed-loop cross-system workflows, SMART v2 scopes with ABAC and SCIM, and a
clinical API with asynchronous bulk export. Start at
[services/interop/README.md](services/interop/README.md); the
[Phase 6 engineering report](docs/design/phase-6-engineering-report.md) records the benchmarks,
the three security-relevant Blockers the end-to-end run found, and an honest readiness
assessment.

**Nothing in Phase 6 has exchanged a message with a real hospital system.** Every conformance
claim is against a specification document, not against a counterparty.

**Phase 7's analytics layer is implemented and tested**: a dimensional warehouse of seven
facts over six conformed dimensions, a watermarked idempotent ETL that de-identifies at load so
the warehouse holds no direct identifiers, a semantic layer of 18 metrics declared as versioned
data rather than queries, a template-only query surface with typed parameters and no free-form
SQL, statistical disclosure control that suppresses complementarily so a withheld cell cannot be
recovered by subtraction, the four Phase 0 dashboard categories, scheduled report generation with
delivery, and the `/analytics/*` API. Start at
[services/analytics/README.md](services/analytics/README.md); the
[Phase 7 engineering report](docs/design/phase-7-engineering-report.md) records the benchmarks,
the four defects the end-to-end run and adversarial pass found, and an honest readiness
assessment.

**Phase 8 integrated the whole platform**: a composition root that constructs all nine services
with declared dependencies and per-service criticality, a ten-stage end-to-end workflow carrying
one correlation id from document upload to a recorded analytics fact, a declared route registry
validated against the running services, four-part startup validation that fails fast, and static
validation of every deployment asset. No new capability was added.

It also found what seven phases of green suites had missed. The container caught eight interface
assumptions that had drifted between services. The deployment validator caught an image shipping
six of nine packages — one that builds, starts, passes its health check, and cannot import a third
of the platform. Two settings systems turned out to disagree about the word "production" while
every deployment asset used the long form, so every containerised start failed at settings load.
Secrets were mounted where nothing read them. And Phase 6's nine FHIR operations and Phase 7's
eight analytics operations, both fully implemented and tested, were mounted nowhere at all —
from an operator's position, dead code. All are fixed and mounted; the
[Phase 8 engineering report](docs/design/phase-8-engineering-report.md) records each one, including
two privilege escalations found by adversarial review of Phase 8's own code.

The **web UI** is not implemented, and the analytics loaders are not yet wired to the Phase 1–6
stores — the warehouse contract is exercised against generated extracts, and `health()` reports
`503 warehouse-empty` rather than answering every question with zero. Four substitutions are outstanding
by design and named in the reports rather than papered over: the embedding provider is a
deterministic lexical baseline, the reranker is a linear feature scorer, the language model is
a deterministic extractive composer.

**The fourth substitution is now partly retired.** Until Phase 9 W6 nothing had run against real
infrastructure, and the integration suite that was supposed to prove otherwise had never
executed — green and inert since Phase 1. CI now starts PostgreSQL, Redis, MongoDB, Neo4j, and
Kafka and runs 55 tests against them, and a run that reaches none of them fails rather than
passing quietly. What is still unproven is stated in
[docs/testing/integration-testing.md](docs/testing/integration-testing.md): managed services,
Atlas vector search, multi-broker Kafka, Neo4j clustering, Kubernetes, and sustained load. The
[roadmap](docs/roadmap/implementation-roadmap.md) lists precisely what shipped, what was
deferred and why.

```bash
make install && make services-up && make migrate && make api   # http://localhost:8000/docs
make check                                                      # lint + type-check + tests
```

Against real backing services — `make test-role` is not optional, because it creates the
non-superuser role without which every row-level-security assertion passes vacuously:

```bash
make services-up && make migrate && make test-role && make test-integration
```

## Why three retrieval modalities

A single vector index cannot do multi-hop clinical reasoning ("adverse events for drugs that
treat condition X in patients also taking drug Y") or guarantee the exact-match precision
clinicians expect on drug names and codes. CIP fuses:

1. **Structured retrieval** — normalized clinical facts (FHIR-shaped), tenant/RBAC metadata
2. **Semantic retrieval** — hybrid dense-vector + BM25 keyword search over unstructured clinical text and literature
3. **Graph retrieval** — a clinical knowledge graph (SNOMED CT / ICD / LOINC / RxNorm / UMLS-linked entities) with GraphRAG-style local (entity) and global (community-summary) search

Full rationale: [ADR-0001](docs/design/adr-0001-hybrid-graph-vector-retrieval.md).

## Documentation map

| Area | Document |
|---|---|
| **Ingestion service (Phase 1 implementation)** | [services/ingestion/README.md](services/ingestion/README.md) |
| **Retrieval service (Phase 2 implementation)** | [services/retrieval/README.md](services/retrieval/README.md) |
| **Copilot service (Phase 3 implementation)** | [services/copilot/README.md](services/copilot/README.md) |
| **Platform library (Phase 4 implementation)** | [libs/cip_platform/README.md](libs/cip_platform/README.md) |
| **Decision service (Phase 5 implementation)** | [services/decision/README.md](services/decision/README.md) |
| **Interop service (Phase 6 implementation)** | [services/interop/README.md](services/interop/README.md) |
| **Analytics service (Phase 7 implementation)** | [services/analytics/README.md](services/analytics/README.md) |
| **Gateway composition root** | [services/gateway/README.md](services/gateway/README.md) |
| **Integration testing** (what runs against real infrastructure, and what does not) | [docs/testing/integration-testing.md](docs/testing/integration-testing.md) |
| Analytics warehouse design | [docs/architecture/11-analytics-warehouse.md](docs/architecture/11-analytics-warehouse.md) |
| Clinical ecosystem interoperability design | [docs/architecture/10-clinical-ecosystem-interoperability.md](docs/architecture/10-clinical-ecosystem-interoperability.md) |
| **HL7 v2 / FHIR mapping reference** | [docs/integration/hl7-fhir-mapping.md](docs/integration/hl7-fhir-mapping.md) |
| Multi-region deployment & disaster recovery | [docs/deployment/multi-region-dr.md](docs/deployment/multi-region-dr.md) |
| Clinical decision intelligence design | [docs/architecture/09-clinical-decision-intelligence.md](docs/architecture/09-clinical-decision-intelligence.md) |
| **Clinical safety case** (limitations, hazards, controls) | [docs/safety/clinical-safety-case.md](docs/safety/clinical-safety-case.md) |
| Production platform design | [docs/architecture/08-production-platform.md](docs/architecture/08-production-platform.md) |
| Operational runbooks (one per alert) | [docs/operations/runbooks/](docs/operations/runbooks/) |
| Clinical copilot design | [docs/architecture/07-clinical-copilot.md](docs/architecture/07-clinical-copilot.md) |
| **Phase 2 engineering report** (benchmarks, bugs, readiness) | [docs/design/phase-2-engineering-report.md](docs/design/phase-2-engineering-report.md) |
| **Phase 3 engineering report** (benchmarks, bugs, readiness) | [docs/design/phase-3-engineering-report.md](docs/design/phase-3-engineering-report.md) |
| **Phase 4 engineering report** (benchmarks, bugs, readiness) | [docs/design/phase-4-engineering-report.md](docs/design/phase-4-engineering-report.md) |
| **Phase 5 engineering report** (benchmarks, bugs, readiness) | [docs/design/phase-5-engineering-report.md](docs/design/phase-5-engineering-report.md) |
| **Phase 6 engineering report** (benchmarks, bugs, readiness) | [docs/design/phase-6-engineering-report.md](docs/design/phase-6-engineering-report.md) |
| **Phase 7 engineering report** (benchmarks, bugs, readiness) | [docs/design/phase-7-engineering-report.md](docs/design/phase-7-engineering-report.md) |
| **Phase 8 engineering report** (integration defects, readiness) | [docs/design/phase-8-engineering-report.md](docs/design/phase-8-engineering-report.md) |
| System architecture (context/container diagrams, service inventory) | [docs/architecture/01-system-architecture.md](docs/architecture/01-system-architecture.md) |
| RAG & hybrid retrieval design | [docs/architecture/02-rag-hybrid-retrieval.md](docs/architecture/02-rag-hybrid-retrieval.md) |
| Knowledge graph design | [docs/architecture/03-knowledge-graph.md](docs/architecture/03-knowledge-graph.md) |
| Conversational AI design | [docs/architecture/04-conversational-ai.md](docs/architecture/04-conversational-ai.md) |
| Analytics & dashboard design | [docs/architecture/05-analytics-dashboard.md](docs/architecture/05-analytics-dashboard.md) |
| **System integration** (composition root, pipeline, route registry) | [docs/architecture/12-system-integration.md](docs/architecture/12-system-integration.md) |
| Security, multi-tenancy & HIPAA compliance | [docs/architecture/06-security-compliance.md](docs/architecture/06-security-compliance.md) |
| Architecture Decision Records | [docs/design/](docs/design/) |
| PostgreSQL schema (DDL) | [docs/database/postgres-schema.sql](docs/database/postgres-schema.sql) |
| Neo4j graph schema | [docs/database/graph-schema.md](docs/database/graph-schema.md) |
| API specification (OpenAPI 3.0) | [docs/api/openapi.yaml](docs/api/openapi.yaml) |
| Deployment architecture | [docs/deployment/deployment-architecture.md](docs/deployment/deployment-architecture.md) |
| SLA, disaster recovery, incident response & cost model | [docs/operations/sla-dr.md](docs/operations/sla-dr.md) |
| Tenant lifecycle (onboarding/offboarding) | [docs/operations/tenant-lifecycle.md](docs/operations/tenant-lifecycle.md) |
| Ontology licensing (SNOMED CT/UMLS/RxNorm) | [docs/legal/ontology-licensing.md](docs/legal/ontology-licensing.md) |
| Glossary | [docs/glossary.md](docs/glossary.md) |
| Non-functional requirements (consolidated latency/scale targets) | [docs/nfr.md](docs/nfr.md) |
| Implementation roadmap | [docs/roadmap/implementation-roadmap.md](docs/roadmap/implementation-roadmap.md) |
| **Phase 0 architecture review (findings) & sign-off** | [docs/design/phase-0-architecture-review.md](docs/design/phase-0-architecture-review.md) · [docs/roadmap/phase0-signoff.md](docs/roadmap/phase0-signoff.md) |

## Repository structure

Directories marked **✅** exist and are implemented; the rest are the target layout this
repository grows into across Phases 2+
([docs/roadmap/implementation-roadmap.md](docs/roadmap/implementation-roadmap.md)).

```
clinical-intelligence-platform/
├── README.md
├── docs/
│   ├── architecture/
│   │   ├── 01-system-architecture.md
│   │   ├── 02-rag-hybrid-retrieval.md
│   │   ├── 03-knowledge-graph.md
│   │   ├── 04-conversational-ai.md
│   │   ├── 05-analytics-dashboard.md
│   │   └── 06-security-compliance.md
│   ├── design/                        # Architecture Decision Records + review record
│   │   ├── adr-0001-hybrid-graph-vector-retrieval.md
│   │   ├── adr-0002-graph-database-choice.md
│   │   ├── adr-0003-multi-tenancy-model.md
│   │   ├── adr-0004-storage-engine-evaluation.md
│   │   ├── adr-0005-phase1-service-decomposition.md
│   │   ├── adr-0006-phase1-chunking-strategy.md
│   │   ├── adr-0007-vector-store-mongodb-atlas.md
│   │   ├── adr-0008-copilot-module-boundaries.md
│   │   ├── adr-0009-deterministic-orchestration.md
│   │   ├── adr-0010-verification-not-self-critique.md
│   │   ├── adr-0011-memory-tiers.md
│   │   ├── adr-0012-language-model-seam.md
│   │   ├── phase-0-architecture-review.md
│   │   ├── phase-2-engineering-report.md
│   │   └── phase-3-engineering-report.md
│   ├── database/
│   │   ├── postgres-schema.sql
│   │   └── graph-schema.md
│   ├── api/
│   │   └── openapi.yaml
│   ├── deployment/
│   │   └── deployment-architecture.md
│   ├── operations/                    # SLA/DR, incident response, tenant lifecycle
│   │   ├── sla-dr.md
│   │   └── tenant-lifecycle.md
│   ├── legal/
│   │   └── ontology-licensing.md
│   ├── roadmap/
│   │   ├── implementation-roadmap.md
│   │   └── phase0-signoff.md
│   ├── glossary.md
│   └── nfr.md
│
├── libs/
│   ├── cip_core/                    ✅ Shared platform primitives: config, logging,
│   │                                   errors, tenancy, audit, storage, DB connections.
│   │                                   One implementation of ADR-0003's tenant-context
│   │                                   rule, not nine reimplementations.
│   └── cip_platform/                ✅ Phase 4 production platform library
│       └── src/cip_platform/
│           ├── cache/                  Five cache domains, tenant-scoped keys
│           ├── events/                 Event spine, audit-emitting bus, outbox
│           ├── tasks/                  Workers, classified retries, dead-lettering
│           ├── observability/          OTel GenAI conventions, metrics, tracing
│           ├── security/               API keys, RBAC, rate limits, spend budgets
│           └── mlops/                  Model and evaluation registries
├── services/
│   ├── ingestion/                   ✅ Phase 1 document-intelligence pipeline
│   │   └── src/cip_ingestion/
│   │       ├── parsers/                PDF/DOCX/text + OCR engine abstraction
│   │       ├── processing/             Normalisation, sections, metadata, chunking, quality
│   │       ├── repositories/           Tenant-scoped persistence
│   │       ├── api/                    FastAPI app, auth skeleton, middleware, routes
│   │       ├── processor.py            Pure stage orchestration (no I/O)
│   │       ├── pipeline.py             Full ETL with storage/database/audit I/O
│   │       └── cli.py                  Batch ingest, health, config, migrations
│   ├── retrieval/                   ✅ Phase 2 hybrid retrieval intelligence layer
│   │   └── src/cip_retrieval/
│   │       ├── embeddings/             Provider protocol, batching, retry, cache, versioning
│   │       ├── vectorstore/            Atlas `$vectorSearch` + exact in-memory backend
│   │       ├── graph/                  Ontology-aware nodes/edges, provenance, traversal
│   │       ├── retrievers/             Vector, BM25 keyword, graph
│   │       ├── prompts/                Versioned template registry (YAML)
│   │       ├── evaluation/             Retrieval + grounding metrics, eval harness
│   │       ├── fusion.py               Weighted Reciprocal Rank Fusion
│   │       ├── routing.py              Intent classification → strategy weights
│   │       ├── reranking.py            Interpretable feature reranker
│   │       ├── context.py              Token budget, dedup, citations, graph evidence
│   │       ├── pipeline.py             Orchestration + no-evidence gate
│   │       └── demo.py                 End-to-end verification, benchmarks, evaluation
│   ├── copilot/                     ✅ Phase 3 clinical intelligence layer
│   │   └── src/cip_copilot/
│   │       ├── domain.py               Evidence, Claim, CopilotState, Answer
│   │       ├── records.py              FHIR-shaped clinical records + data-source protocol
│   │       ├── llm/                    LanguageModel seam + extractive implementation
│   │       ├── prompts/                Registry v2: pins, rollback, experiments
│   │       ├── memory/                 Working / episodic / semantic tiers
│   │       ├── timeline/               Chronological reconstruction
│   │       ├── tools/                  Ten clinical tools behind one registry
│   │       ├── planner/                Question → validated Plan
│   │       ├── reasoning/              Evidence aggregation → claims
│   │       ├── validation/             Claim verification (the reflection pass)
│   │       ├── safety/                 Five clinical safety detectors
│   │       ├── explanations/           Evidence, graph chains, trace, confidence
│   │       ├── output/                 Markdown / JSON / API / FHIR renderers
│   │       ├── agents/                 The eight pipeline stages
│   │       ├── evaluation/             Reasoning, planning, cost metrics
│   │       ├── orchestrator.py         Stage sequencing + HITL suspend/resume
│   │       └── demo.py                 End-to-end verification and benchmarks
│   ├── decision/                    ✅ Phase 5 clinical decision intelligence
│   │   └── src/cip_decision/
│   │       ├── domain.py               Facts, severity, evidence quality, provenance
│   │       ├── rules/                  Typed condition AST (no `eval`) + evaluator
│   │       ├── knowledge/              Strict loader + the versioned cited YAML corpus
│   │       ├── drugs/                  Seven drug-safety checks
│   │       ├── risk/                   Risk models that refuse to score out of population
│   │       ├── pathways/               FHIR PlanDefinition-shaped care pathways
│   │       ├── suppression.py          Dedup, override memory, role floor, ceiling
│   │       ├── contradiction.py        Declared-direction conflict detection
│   │       ├── evidence_graph/         Guideline → rule → fact → recommendation paths
│   │       ├── approval/               The review lifecycle nothing bypasses
│   │       ├── hooks/                  CDS Hooks 2.0 discovery, services, cards
│   │       ├── smart/                  SMART-on-FHIR launch context
│   │       ├── engine.py               The decision pipeline
│   │       ├── workflow/               Event-driven clinical runs
│   │       ├── evaluation/             Accuracy, alert burden, rule coverage
│   │       └── demo.py                 End-to-end verification, evaluation, benchmarks
│   ├── interop/                     ✅ Phase 6 clinical ecosystem interoperability
│   │   └── src/cip_interop/
│   │       ├── domain.py               Identifiers, names, purpose of use, source records
│   │       ├── hl7/                    MLLP, delimiter-aware parser, validation, ACK
│   │       ├── fhir/                   Definitions, validation, versioned store, bundles
│   │       ├── orgs.py                 Organisation hierarchy + sharing agreements
│   │       ├── mapping/                Declarative HL7 to FHIR maps + transforms
│   │       ├── empi/                   Fellegi-Sunter matching, review, merge, split
│   │       ├── imaging.py              DICOM identity, PACS refs, worklist reconciliation
│   │       ├── consent.py              Deny-by-default disclosure decisions, break-glass
│   │       ├── security.py             SMART v2 scopes, ABAC, SCIM, delegation
│   │       ├── streaming.py            Per-person partitions, idempotent consumers, replay
│   │       ├── routing.py              Channels, retries, dead letters, ingest pipeline
│   │       ├── population.py           Cohorts, prevalence, risk bands, quality measures
│   │       ├── datalake.py             Bronze/silver/gold, Safe Harbor, feature store
│   │       ├── dashboards.py           Four audience projections over the stream
│   │       ├── workflow.py             Closed-loop referrals, orders, discharge
│   │       ├── api.py                  REST, FHIR, bulk export and import
│   │       └── demo.py                 End-to-end run, load simulation, benchmarks
│   ├── analytics/                   ✅ Phase 7 analytics warehouse & reporting
│   │   └── src/cip_analytics/
│   │       ├── domain.py               Measures, additivity, freshness, disclosure policy
│   │       ├── warehouse.py            Star schema + typed tenant-scoped store
│   │       ├── etl.py                  Watermarked, idempotent, de-identifying loads
│   │       ├── semantic.py             Metric declarations + registry
│   │       ├── disclosure.py           Primary + complementary cell suppression
│   │       ├── query.py                Typed templates, execution, suppression
│   │       ├── boards.py               The four dashboard categories
│   │       ├── reports.py              Schedules, rendering, delivery
│   │       ├── api.py                  /analytics/* read-only surface
│   │       ├── metrics/catalogue.yaml  18 metric declarations
│   │       └── demo.py                 End-to-end run, attacks, benchmarks
│   └── gateway/                     ✅ Composition root: health, jobs, scheduler, worker
├── migrations/                      ✅ Alembic migrations for the operational store
├── tests/                           ✅ unit / api / integration / retrieval / copilot /
│                                       platform / decision / interop / analytics
│
│   # --- later-phase target layout; not yet created ---
│
├── services/identity/                  AuthN/Z, tenant & RBAC/ABAC, break-glass grants
├── services/extraction/                Ontology coding, LocalConcept fallback
├── services/knowledge-graph/           Entity extraction, community detection
├── services/analytics/                 Aggregation, cohort queries, dashboard API
├── web/                                Chat UI, search, dashboards, admin console
├── infra/                              IaC (Kubernetes / Terraform), CI/CD, policy gates
└── eval/                               Offline retrieval/generation evaluation suite
    ├── datasets/                       SME-authored Q&A pairs per tenant vertical
    ├── harness/                        Eval runner (RAGAS-style + numeric accuracy), CI-gated
    └── reports/                        Historical eval runs, tracked for drift
```

Tests live in a single top-level `tests/` tree (`unit/`, `api/`, `integration/`, `retrieval/`,
`copilot/`) rather than
per-service, because Phase 1 ships one deployable unit
([ADR-0005](docs/design/adr-0005-phase1-service-decomposition.md)). They move alongside their
service when the first extraction happens.

## Technology stack (selected in Phase 0, revised where implementation proved otherwise)

| Layer | Choice | Reference |
|---|---|---|
| Operational store | PostgreSQL 15+ with pgvector | [postgres-schema.sql](docs/database/postgres-schema.sql) |
| Graph store | Neo4j (Aura managed / self-hosted) | [ADR-0002](docs/design/adr-0002-graph-database-choice.md) |
| Keyword/BM25 index | OpenSearch | [02-rag-hybrid-retrieval.md](docs/architecture/02-rag-hybrid-retrieval.md) |
| Analytics warehouse | BigQuery / Synapse / Redshift (per cloud provider) | [05-analytics-dashboard.md](docs/architecture/05-analytics-dashboard.md) |
| Event bus | Kafka (or managed equivalent) | [01-system-architecture.md](docs/architecture/01-system-architecture.md) |
| Retrieval/orchestration framework | LlamaIndex-style indexing, LangGraph-style agent orchestration | [02-rag-hybrid-retrieval.md §3](docs/architecture/02-rag-hybrid-retrieval.md#3-orchestration-framework-choice) |
| Embedding model | Selected via Phase 1 bake-off; model-agnostic registry | [postgres-schema.sql](docs/database/postgres-schema.sql) `embedding_models` |
| Clinical ontologies | SNOMED CT, ICD-10/11, LOINC, RxNorm, HPO — reconciled via UMLS CUI | [03-knowledge-graph.md](docs/architecture/03-knowledge-graph.md) |
| Interoperability | HL7 FHIR R4, HL7v2, C-CDA, DICOM | [02-rag-hybrid-retrieval.md §1.1](docs/architecture/02-rag-hybrid-retrieval.md#11-document-classification--layout-aware-parsing) |
| Deployment | Kubernetes (EKS/AKS/GKE or on-prem), cloud + hybrid + on-prem topologies | [deployment-architecture.md](docs/deployment/deployment-architecture.md) |
| Compliance baseline | HIPAA, HITRUST CSF, SOC 2, 21 CFR Part 11 (pharma) | [06-security-compliance.md](docs/architecture/06-security-compliance.md) |
| Document/chunk/vector storage engine | Postgres + pgvector + OpenSearch, evaluated against a unified MongoDB Atlas store and kept as-is | [ADR-0004](docs/design/adr-0004-storage-engine-evaluation.md) |
| Connection pooling (schema-per-tenant) | Tenant-sharded PgBouncer session-pooling tier | [ADR-0003 §Consequences](docs/design/adr-0003-multi-tenancy-model.md#consequences) |
| MongoDB's role | Parsed-document artifact store (pre-chunking, format-dependent, write-once) | [ADR-0005](docs/design/adr-0005-phase1-service-decomposition.md) |
| Phase 1 chunking | Structural (section/table/sentence aware) behind the `Chunker` protocol; embedding-similarity chunking implements the same protocol in Phase 2 | [ADR-0006](docs/design/adr-0006-phase1-chunking-strategy.md) |
| Phase 1 runtime | Python 3.11, FastAPI, SQLAlchemy 2 async, Alembic, structlog, pytest | [services/ingestion/README.md](services/ingestion/README.md) |
| Vector tier (Phase 2) | MongoDB Atlas Vector Search, superseding ADR-0004's pgvector choice | [ADR-0007](docs/design/adr-0007-vector-store-mongodb-atlas.md) |
| Rank fusion | Reciprocal Rank Fusion (k=60) over per-strategy rankings | [phase-2-engineering-report.md §1.2](docs/design/phase-2-engineering-report.md) |
| Agent orchestration (Phase 3) | Deterministic stage pipeline; no agent framework | [ADR-0009](docs/design/adr-0009-deterministic-orchestration.md), [ADR-0012](docs/design/adr-0012-language-model-seam.md) |
| Reflection | Verification against cited evidence, not LLM self-critique | [ADR-0010](docs/design/adr-0010-verification-not-self-critique.md) |

## License

Proprietary — architecture design phase, not yet licensed for distribution.
