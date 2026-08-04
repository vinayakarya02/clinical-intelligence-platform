# Clinical Intelligence Platform (CIP)

An enterprise-grade Clinical Intelligence & Analytics Platform for hospitals, pharmaceutical
companies, and healthcare analytics organizations — combining a clinical knowledge graph, hybrid
(vector + keyword + graph) retrieval-augmented generation, grounded conversational AI, and an
analytics/BI layer over the same governed, access-controlled data substrate.

This is not a chatbot demo and not a simple single-store RAG pipeline. It is designed as a
multi-tenant system with defense-in-depth data isolation, HIPAA-aligned compliance controls, and
provenance/citation on every generated answer — see [docs/architecture/06-security-compliance.md](docs/architecture/06-security-compliance.md).

## Status: Phase 2 — Hybrid retrieval intelligence layer implemented

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

Conversational AI, the analytics layer, and the web UI are **not** implemented — they are
Phases 2's conversational half and Phase 3+. Two substitutions inside Phase 2 are also
outstanding by design: the embedding provider is a deterministic lexical baseline rather
than a clinical model, and the reranker is a linear feature scorer rather than the
cross-encoder Phase 0 specifies. The
[roadmap](docs/roadmap/implementation-roadmap.md) lists precisely what shipped, what was
deferred and why.

```bash
make install && make services-up && make migrate && make api   # http://localhost:8000/docs
make check                                                      # lint + type-check + tests
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
| **Phase 2 engineering report** (benchmarks, bugs, readiness) | [docs/design/phase-2-engineering-report.md](docs/design/phase-2-engineering-report.md) |
| System architecture (context/container diagrams, service inventory) | [docs/architecture/01-system-architecture.md](docs/architecture/01-system-architecture.md) |
| RAG & hybrid retrieval design | [docs/architecture/02-rag-hybrid-retrieval.md](docs/architecture/02-rag-hybrid-retrieval.md) |
| Knowledge graph design | [docs/architecture/03-knowledge-graph.md](docs/architecture/03-knowledge-graph.md) |
| Conversational AI design | [docs/architecture/04-conversational-ai.md](docs/architecture/04-conversational-ai.md) |
| Analytics & dashboard design | [docs/architecture/05-analytics-dashboard.md](docs/architecture/05-analytics-dashboard.md) |
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
│   │   ├── phase-0-architecture-review.md
│   │   └── phase-2-engineering-report.md
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
│   └── cip_core/                    ✅ Shared platform primitives: config, logging,
│                                       errors, tenancy, audit, storage, DB connections.
│                                       One implementation of ADR-0003's tenant-context
│                                       rule, not nine reimplementations.
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
│   └── retrieval/                   ✅ Phase 2 hybrid retrieval intelligence layer
│       └── src/cip_retrieval/
│           ├── embeddings/             Provider protocol, batching, retry, cache, versioning
│           ├── vectorstore/            Atlas `$vectorSearch` + exact in-memory backend
│           ├── graph/                  Ontology-aware nodes/edges, provenance, traversal
│           ├── retrievers/             Vector, BM25 keyword, graph
│           ├── prompts/                Versioned template registry (YAML)
│           ├── evaluation/             Retrieval + grounding metrics, eval harness
│           ├── fusion.py               Weighted Reciprocal Rank Fusion
│           ├── routing.py              Intent classification → strategy weights
│           ├── reranking.py            Interpretable feature reranker
│           ├── context.py              Token budget, dedup, citations, graph evidence
│           ├── pipeline.py             Orchestration + no-evidence gate
│           └── demo.py                 End-to-end verification, benchmarks, evaluation
├── migrations/                      ✅ Alembic migrations for the operational store
├── tests/                           ✅ unit / api / integration
│
│   # --- later-phase target layout; not yet created ---
│
├── services/identity/                  AuthN/Z, tenant & RBAC/ABAC, break-glass grants
├── services/extraction/                Ontology coding, LocalConcept fallback
├── services/knowledge-graph/           Entity extraction, community detection
├── services/conversational-ai/         Chat orchestration, grounding, numeric verification
├── services/analytics/                 Aggregation, cohort queries, dashboard API
├── web/                                Chat UI, search, dashboards, admin console
├── infra/                              IaC (Kubernetes / Terraform), CI/CD, policy gates
└── eval/                               Offline retrieval/generation evaluation suite
    ├── datasets/                       SME-authored Q&A pairs per tenant vertical
    ├── harness/                        Eval runner (RAGAS-style + numeric accuracy), CI-gated
    └── reports/                        Historical eval runs, tracked for drift
```

Tests live in a single top-level `tests/` tree (`unit/`, `api/`, `integration/`) rather than
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

## License

Proprietary — architecture design phase, not yet licensed for distribution.
