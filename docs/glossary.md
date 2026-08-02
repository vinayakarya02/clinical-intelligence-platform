# Glossary

**Status:** Phase 0 — Design only
**Added:** Phase 0 review found terms used throughout the document set with no single reference —
see [design/phase-0-architecture-review.md](design/phase-0-architecture-review.md) finding A14.

| Term | Meaning |
|---|---|
| **ABAC** | Attribute-Based Access Control — access decisions based on attributes (department, patient panel, identification level), layered on top of coarse RBAC roles. See [06-security-compliance.md §3](architecture/06-security-compliance.md#3-identity-authentication--authorization). |
| **BAA** | Business Associate Agreement — a HIPAA-required contract with any vendor/subprocessor that touches PHI on the platform's behalf. |
| **Bi-temporal** | Tracking both clinical (valid) time and transaction (recorded) time for a fact, so "what was true" and "what we believed, and when" can both be queried. See [graph-schema.md §1](database/graph-schema.md#1-node-labels--properties). |
| **BM25** | Best Matching 25 — the standard sparse/keyword ranking function behind OpenSearch full-text search, complementary to dense vector search. |
| **Break-glass** | Emergency access override letting a clinician access a patient record outside their normal panel, with mandatory heightened audit. See [openapi.yaml](api/openapi.yaml) `/access/break-glass`. |
| **CUI** | Concept Unique Identifier — UMLS's canonical ID for a clinical concept, used to reconcile the same concept across SNOMED CT, ICD, RxNorm, LOINC, etc. |
| **DRIFT search** | A GraphRAG query mode that starts at the community (global) level to locate relevant context, then "drifts" into local entity-level search for detail. |
| **GraphRAG** | Retrieval-Augmented Generation architecture (originated by Microsoft) that builds a knowledge graph from documents, detects communities, and summarizes them to answer both entity-specific (local) and thematic (global) queries. |
| **HNSW** | Hierarchical Navigable Small World — the approximate-nearest-neighbor index algorithm used by pgvector for vector similarity search. |
| **Leiden algorithm** | A hierarchical community-detection algorithm used to cluster densely-connected entities in the knowledge graph for GraphRAG-style community summarization. |
| **LocalConcept** | A graph node type for entities that fail to resolve to any standard ontology concept (UMLS CUI) — an explicit fallback rather than silent data loss. See [graph-schema.md §1](database/graph-schema.md#1-node-labels--properties). |
| **PHI** | Protected Health Information — individually identifiable health information covered by HIPAA. |
| **RBAC** | Role-Based Access Control — coarse-grained access by role (`admin`, `clinician`, `analyst`, etc.). |
| **RLS** | Row-Level Security — PostgreSQL's native mechanism for enforcing per-row access policies (used here for tenant isolation) at the database engine level, not the application layer. |
| **RRF** | Reciprocal Rank Fusion — the standard technique for combining ranked result lists from multiple retrieval methods (vector, keyword, graph) into one fused ranking. |
| **Safe Harbor** | A HIPAA de-identification method: removal of 18 specified identifier categories. |
| **Expert Determination** | The alternative HIPAA de-identification method: a documented statistical assessment that re-identification risk is very small, used when Safe Harbor would strip data needed for research/analytics. |
| **TCB (trusted computing base)** | The minimal set of components whose failure compromises the whole system's security guarantees. The Retrieval & Orchestration Service is explicitly named the platform's TCB for tenant/RBAC filtering. See [01-system-architecture.md §2](architecture/01-system-architecture.md#2-design-principles). |
| **UMLS** | Unified Medical Language System — NLM's meta-thesaurus integrating SNOMED CT, RxNorm, LOINC, ICD, and 200+ other vocabularies via a shared CUI. |
| **WORM** | Write-Once-Read-Many — immutable storage used for tamper-evident audit log archival. |

## Related documents

- [Non-Functional Requirements](nfr.md)
- [Phase 0 Architecture Review](design/phase-0-architecture-review.md)
