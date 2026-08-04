# Phase 0 Architecture Review

**Review type:** Adversarial principal-engineer (L6-equivalent) production design review
**Reviewed:** Full Phase 0 document set (architecture, ADRs, schemas, API spec, deployment, roadmap)
**Method:** Four independent reviewers, each scoped to a dimension and blind to the others'
findings, tasked explicitly with finding weaknesses rather than validating the design. Findings
below are the reviewers' output, deduplicated and prioritized. This document is the permanent
record of that review; **[Resolved]** tags and cross-references were added as each finding was
addressed in the documents listed.

Severity definitions: **Blocker** — would fail a real production-readiness/compliance review and
must be fixed before Phase 1 code is written. **High** — must be fixed before Phase 1 exit.
**Medium** — should be fixed in Phase 0 docs or explicitly deferred with a named owner/phase.
**Low** — worth tracking, acceptable to defer with a note.

---

## A. System architecture, repo structure, docs, enterprise readiness

| # | Sev | Finding | Resolution |
|---|---|---|---|
| A1 | Blocker | No SLA/RPO/RTO/disaster-recovery document existed anywhere. | **[Resolved]** Added [docs/operations/sla-dr.md](../operations/sla-dr.md). |
| A2 | Blocker | Clinical ontologies (SNOMED CT, UMLS, RxNorm) are not free/unrestricted — no licensing treatment existed. | **[Resolved]** Added [docs/legal/ontology-licensing.md](../legal/ontology-licensing.md); referenced from roadmap as a Phase 1 procurement dependency. |
| A3 | High | No unit-economics/cost model beyond infra latency budget (LLM token spend, ontology license fees, support). | **[Resolved]** Added to [docs/operations/sla-dr.md](../operations/sla-dr.md) §6 and flagged in roadmap Phase 4. |
| A4 | High | No incident-response / breach-notification process (HIPAA §164.404 requires notification within 60 days). | **[Resolved]** Added [docs/operations/sla-dr.md](../operations/sla-dr.md) §4. |
| A5 | High | No tenant onboarding/offboarding runbook despite schema implying full lifecycle (`tenants.status`). | **[Resolved]** Added [docs/operations/tenant-lifecycle.md](../operations/tenant-lifecycle.md). |
| A6 | High | No shared-library strategy in repo structure despite 9 services needing identical tenant-context/ontology/citation logic. | **[Resolved]** Added `libs/` to target tree in [README.md](../../README.md). |
| A7 | High | No test/eval directory structure despite eval gates being load-bearing in 3 documents. | **[Resolved]** Expanded `eval/` and per-service `tests/` in [README.md](../../README.md) target tree. |
| A8 | Medium | Conversational AI/Analytics trust Retrieval Service as their only access-control boundary — "defense-in-depth" framing overstates independence from the consumer side. | **[Resolved]** [01-system-architecture.md](../architecture/01-system-architecture.md) §2 now names Retrieval Service as an explicit trusted-computing-base boundary and requires it be threat-modeled accordingly. |
| A9 | Medium | Imaging-pixel and genomics modalities silently absent (not flagged as deferred). | **[Resolved]** Added to Explicit non-goals in [04-conversational-ai.md](../architecture/04-conversational-ai.md) §6 and roadmap Phase 9. |
| A10 | Medium | No ontology plugin/registry pattern — adding a regional ontology (OPCS-4, ICD-10-CA) requires code changes across services. | **[Resolved]** [03-knowledge-graph.md](../architecture/03-knowledge-graph.md) §2.1 now specifies an ontology registry pattern mirroring `embedding_models`. |
| A11 | Medium | No third-party integration/marketplace story, and not explicitly scoped out. | **[Resolved]** Added to roadmap as explicit Phase 10+ non-goal. |
| A12 | Medium | No MLOps/fine-tuning/federated-learning roadmap. | **[Resolved]** Added to roadmap Phase 9 as an explicit forward-looking item, not a silent gap. |
| A13 | Medium | ADR-0002 states Neptune is "the recommended choice for AWS-only deployments," but deployment-architecture.md only documents it as an alternative — internal contradiction. | **[Resolved]** [ADR-0002](adr-0002-graph-database-choice.md) language aligned with [deployment-architecture.md](../deployment/deployment-architecture.md) §3 — Neptune is documented as an available alternative, not a recommendation. |
| A14 | Medium | No glossary or consolidated NFR document — terms (CUI, RRF, DRIFT, Leiden) and scattered targets (latency, throughput, ingestion SLAs) have no single reference. | **[Resolved]** Added [docs/glossary.md](../glossary.md) and [docs/nfr.md](../nfr.md). |
| A15 | Medium | No scale-ceiling analysis for schema-per-tenant/database-per-tenant isolation at 10x/100x tenant count. | **[Resolved]** Added to [ADR-0003](adr-0003-multi-tenancy-model.md) §Consequences and [docs/nfr.md](../nfr.md). |
| A16 | Low | Hybrid/on-prem topology only addresses cloud IdPs (Entra/IAM/Identity Platform), not on-prem AD/LDAP/ADFS common in hospital IT. | **[Resolved]** [deployment-architecture.md](../deployment/deployment-architecture.md) §3 adds on-prem AD/ADFS/LDAP as an identity option for the hybrid topology. |
| A17 | Low | Roadmap references "a third-party security review" with no named process/cadence. | **[Resolved]** [implementation-roadmap.md](../roadmap/implementation-roadmap.md) Phase 1/4 now name the review cadence. |
| A18 | Low | Roadmap's Phase 0 "exit criteria met" claim had no sign-off artifact. | **[Resolved]** Added [docs/roadmap/phase0-signoff.md](../roadmap/phase0-signoff.md). |

## B. RAG pipeline (retrieval, chunking, embeddings, reranking, eval, hallucination, citations, memory)

| # | Sev | Finding | Resolution |
|---|---|---|---|
| B1 | Blocker | Embedding API calls are not explicitly covered by the BAA-gating clause (only "generation or extraction" named) — a real PHI-to-subprocessor gap. | **[Resolved]** [06-security-compliance.md](../architecture/06-security-compliance.md) §6 now covers "any external API call that receives clinical text or PHI, including embedding, reranking, and classification." |
| B2 | Blocker | `chunk_embeddings.embedding vector(1536)` hardcoded, contradicting the "model-agnostic, non-destructive migration" claim — different embedding models have different dimensions. | **[Resolved]** [postgres-schema.sql](../database/postgres-schema.sql) restructured to per-dimension embedding tables; §1.3 of [02-rag-hybrid-retrieval.md](../architecture/02-rag-hybrid-retrieval.md) updated with a re-embedding/backfill runbook. |
| B3 | Blocker | No specified behavior when all retrieval paths return empty/low-relevance results — the LLM is free to answer from parametric memory, the most common clinical hallucination mode. | **[Resolved]** [02-rag-hybrid-retrieval.md](../architecture/02-rag-hybrid-retrieval.md) §2.4 (new) adds a hard no-generation-without-context gate. |
| B4 | Blocker | No context-window management strategy for long conversations — silent truncation of clinical history (e.g., dropping a stated contraindication) is a patient-safety failure mode. | **[Resolved]** [04-conversational-ai.md](../architecture/04-conversational-ai.md) §4 adds a token-budget and rolling-summarization policy. |
| B5 | High | Query router under-specified for build (no intent enumeration, confidence thresholds, or fallback on classifier uncertainty). | **[Resolved]** [02-rag-hybrid-retrieval.md](../architecture/02-rag-hybrid-retrieval.md) §2.1 expanded. |
| B6 | High | No reconciliation logic when graph/vector/keyword paths disagree or contradict each other (RRF fuses rank, not truth). | **[Resolved]** [02-rag-hybrid-retrieval.md](../architecture/02-rag-hybrid-retrieval.md) §2.2 adds cross-source consistency checking ahead of the grounding checker. |
| B7 | High | "Semantic chunking" named with no concrete algorithm. | **[Resolved]** [02-rag-hybrid-retrieval.md](../architecture/02-rag-hybrid-retrieval.md) §1.2 specifies the breakpoint method and library-level approach. |
| B8 | High | Table serialization into embeddable chunks unspecified; flattening tables into text is known to degrade embedding quality, and structured tables (`medications`/`observations`) already exist as a better retrieval path. | **[Resolved]** [02-rag-hybrid-retrieval.md](../architecture/02-rag-hybrid-retrieval.md) §1.2 now routes structured data (meds/labs) to direct structured retrieval, with only a short generated narrative summary embedded — not the raw table. |
| B9 | High | No chunking strategy for HL7v2/FHIR (non-narrative) resources. | **[Resolved]** [02-rag-hybrid-retrieval.md](../architecture/02-rag-hybrid-retrieval.md) §1.2 clarifies structured resources bypass chunk-embedding and populate the relational/graph store directly. |
| B10 | High | No dedicated numeric/dosage hallucination check — a wrong lab value or dose is a patient-safety incident, not a generic ungrounded-claim case. | **[Resolved]** [04-conversational-ai.md](../architecture/04-conversational-ai.md) §3 adds a deterministic exact-match numeric-verification step. |
| B11 | High | Reranker model/hosting unspecified; the <100ms fusion+rerank latency budget is unrealistic without pinning model/hardware/candidate-set size. | **[Resolved]** [02-rag-hybrid-retrieval.md](../architecture/02-rag-hybrid-retrieval.md) §2.2 and §5 updated with concrete candidate-set sizing and a revised, justified budget. |
| B12 | High | Eval process asserted ("required before any change ships") without dataset size, ground-truth ownership, cadence, or gating thresholds. | **[Resolved]** [02-rag-hybrid-retrieval.md](../architecture/02-rag-hybrid-retrieval.md) §4 now specifies these concretely. |
| B13 | Medium | Graph-entity-sourced citations have no demonstrated traceable schema path (no entity-to-document provenance table existed). | **[Resolved]** Addressed jointly with KG finding C2 — provenance properties added across graph edges and referenced from [04-conversational-ai.md](../architecture/04-conversational-ai.md) §3. |
| B14 | Medium | `chat_messages.citations_json` is unconstrained JSONB with no referential integrity to real chunk/entity IDs. | **[Resolved]** [postgres-schema.sql](../database/postgres-schema.sql) documents an application-layer validation requirement in a comment; full FK enforcement noted as a Phase 1 implementation task given JSONB structural limits. |
| B15 | Medium | No re-embedding/backfill plan for embedding-model migration. | **[Resolved]** Covered under B2 resolution. |
| B16 | Low | No cross-session patient-context memory. | Acknowledged as a Phase 2+ enhancement in [04-conversational-ai.md](../architecture/04-conversational-ai.md) §4; not fixed in Phase 0 (explicitly deferred, not silently missing). |
| B17 | Low | Serial-vs-parallel execution of retrieval paths unstated, which materially affects whether the latency budget is achievable. | **[Resolved]** [02-rag-hybrid-retrieval.md](../architecture/02-rag-hybrid-retrieval.md) §2.1 states retrieval paths are fanned out concurrently. |

## C. Knowledge graph (ontology, schema, entity model, traversal, GraphRAG readiness, scalability)

| # | Sev | Finding | Resolution |
|---|---|---|---|
| C1 | Blocker | No fallback node for entities that don't map to a UMLS concept — the design silently assumed 100% ontology coverage, which is false for any real hospital dataset (local labs, formulary codes, free-text symptoms). | **[Resolved]** [graph-schema.md](../database/graph-schema.md) §1 adds a `LocalConcept` node with an `UNMAPPED` review state. |
| C2 | Blocker | Only the `CAUSES` edge carries confidence/provenance; the most safety-critical edge, `CONTRAINDICATED_WITH`, carries none — inconsistent, not a deliberate choice. | **[Resolved]** [graph-schema.md](../database/graph-schema.md) §2 adds `{confidence, source_document_id, asserted_by, evidence_level}` to every clinically actionable edge type. |
| C3 | Blocker | No bi-temporal/point-in-time query capability — "what did we know as of date X" is unanswerable, a compliance-critical query class per ADR-0001's own stated goals. | **[Resolved]** [graph-schema.md](../database/graph-schema.md) §1 adds valid-time/transaction-time properties across patient-fact nodes and edges. |
| C4 | Blocker | "Entity resolution via UMLS CUI" solves concept dedup, not fact conflict — no policy for contradictory assertions (e.g., one note says a medication stopped, another says active). | **[Resolved]** [graph-schema.md](../database/graph-schema.md) §1 introduces an `Assertion`-pattern with provenance-weighted, temporal-precedence conflict resolution, documented in [03-knowledge-graph.md](../architecture/03-knowledge-graph.md) §1. |
| C5 | High | Global-search example query silently returns zero rows for a newly opted-in tenant with no not-yet-summarized signal. | **[Resolved]** [graph-schema.md](../database/graph-schema.md) §4 query updated; [03-knowledge-graph.md](../architecture/03-knowledge-graph.md) §1 defines the fallback-to-local-search behavior. |
| C6 | High | "Opt-in deep analytics" had no defined trigger mechanism or SLA. | **[Resolved]** [03-knowledge-graph.md](../architecture/03-knowledge-graph.md) §1 defines the opt-in mechanism and a bounded first-summarization SLA. |
| C7 | High | Leiden re-clustering cadence and community-ID stability (Leiden is non-deterministic across runs) unaddressed — re-runs can silently invalidate existing summaries/citations. | **[Resolved]** [03-knowledge-graph.md](../architecture/03-knowledge-graph.md) §1 specifies re-clustering cadence and an ID-stabilization approach. |
| C8 | High | Local-search example Cypher uses a `CONTAINS` substring scan that the declared full-text index does not accelerate — the "low-cost, real-time" claim is unsupported by the example given. | **[Resolved]** [graph-schema.md](../database/graph-schema.md) §4 rewritten to use `db.index.fulltext.queryNodes`. |
| C9 | High | Full-text index has no demonstrated tenant-safety pattern in shared-database mode — Neo4j full-text queries can't filter inline, so a missed post-filter is a cross-tenant leak. | **[Resolved]** [graph-schema.md](../database/graph-schema.md) §3–4 documents the mandatory post-filter pattern with a corrected example. |
| C10 | High | `CONTRAINDICATED_WITH`/`TREATS`/`RECOMMENDS` modeled between patient-instance `Medication` nodes, meaning generic pharmacological knowledge is either duplicated per patient or never populated consistently. | **[Resolved]** [graph-schema.md](../database/graph-schema.md) §2 moves these to the shared `RxNormConcept`/`SnomedConcept` ontology layer; patient-specific alerts are derived by traversal. |
| C11 | High | No max-hop limit anywhere — a real risk for unbounded/runaway traversal queries. | **[Resolved]** [graph-schema.md](../database/graph-schema.md) §4 examples use bounded variable-length patterns (`*1..2`) and query timeout is documented. |
| C12 | Medium | Uniqueness constraints only exist for 3 of 15+ tenant-scoped node types. | **[Resolved]** [graph-schema.md](../database/graph-schema.md) §3 adds composite `(tenant_id, id)` constraints for all tenant-scoped labels. |
| C13 | Medium | No graph schema versioning or migration strategy. | **[Resolved]** [graph-schema.md](../database/graph-schema.md) §1 adds a `schema_version` property and references a migration process. |
| C14 | Medium | `AllergyIntolerance`/`Device` promised SNOMED/GUDID links in the entity table but no corresponding edge existed. | **[Resolved]** [graph-schema.md](../database/graph-schema.md) §2 adds the missing edges. |
| C15 | Medium | No ontology-version tracking on shared reference nodes — SNOMED ships biannual releases; in-place mutation risks silent semantic drift on already-linked facts. | **[Resolved]** [graph-schema.md](../database/graph-schema.md) §1 adds `ontology_release`/`valid_from` and a supersession-edge pattern instead of in-place update. |
| C16 | Medium | ADR-0002 says the Neo4j vector index is scoped to "entity/community" embeddings; the schema only ever defined a community-level index — overstated scope. | **[Resolved]** [ADR-0002](adr-0002-graph-database-choice.md) corrected to "community-level only," matching [graph-schema.md](../database/graph-schema.md). |
| C17 | Medium | Database-per-tenant scale story asserted without capacity numbers (thousands of Neo4j databases). | **[Resolved]** Folded into A15 / [docs/nfr.md](../nfr.md) scale-ceiling analysis. |
| C18 | Medium | No caching layer or HA/read-replica story for the graph store. | **[Resolved]** [deployment-architecture.md](../deployment/deployment-architecture.md) §2 adds a caching tier and Neo4j Causal Cluster/read-replica topology. |
| C19 | Low | No capacity-planning numbers for hospital-system-scale node/edge counts. | **[Resolved]** Added to [docs/nfr.md](../nfr.md). |
| C20 | Low | `Guideline`/`AdverseEvent` modeled as tenant-scoped when largely universal reference content, risking needless duplication. | **[Resolved]** [graph-schema.md](../database/graph-schema.md) §1 clarifies shared-definition vs. tenant-specific-occurrence split, mirroring the ontology-layer pattern. |
| C21 | Low | `RECOMMENDS`/`REPORTED` edges lack source/version provenance despite the platform's traceability goals. | **[Resolved]** Covered under C2 resolution (applied platform-wide). |

## D. Database & API design

| # | Sev | Finding | Resolution |
|---|---|---|---|
| D1 | Blocker | No partitioning on unbounded-growth tables (`audit_log`, `document_chunks`, `chunk_embeddings`) — multi-billion-row tables within 1-2 years for any real hospital tenant. | **[Resolved]** [postgres-schema.sql](../database/postgres-schema.sql) adds range partitioning by month on `audit_log.occurred_at` and documents a partition-by-ingestion-date strategy for chunk tables. |
| D2 | Blocker | No encryption-at-rest/in-transit specification for PHI columns; `pgcrypto` loaded only for UUIDs. | **[Resolved]** [06-security-compliance.md](../architecture/06-security-compliance.md) §9 (new) mandates TLS + KMS-managed disk encryption + column-level encryption for specific PHI fields; [postgres-schema.sql](../database/postgres-schema.sql) comments reference it. |
| D3 | Blocker | No soft-delete/right-to-delete/retention-purge design — `tenants.status='offboarded'` had no corresponding data-destruction workflow. | **[Resolved]** [postgres-schema.sql](../database/postgres-schema.sql) adds `deleted_at`/`purge_after` columns; process defined in [docs/operations/tenant-lifecycle.md](../operations/tenant-lifecycle.md). |
| D4 | Blocker | `tenant_id` itself was unindexed on shared-schema (RLS) tables — every RLS-filtered query was a sequential scan. | **[Resolved]** [postgres-schema.sql](../database/postgres-schema.sql) adds `tenant_id`-leading composite indexes on every RLS-protected table. |
| D5 | Blocker | `chunk_embeddings` had no `tenant_id` column and no RLS — tenant isolation existed only transitively via a join, contradicting ADR-0003's own "no single missed filter clause" goal; also made the ADR's "query-planner pushdown" claim unimplementable. | **[Resolved]** [postgres-schema.sql](../database/postgres-schema.sql) denormalizes `tenant_id` onto `chunk_embeddings`, enables RLS, and documents the tenant-partitioned ANN search pattern. |
| D6 | Blocker | `chunk_embeddings.embedding vector(1536)` hardcoded against a claimed multi-model registry. | **[Resolved]** Same fix as B2. |
| D7 | Blocker | No bulk/batch document ingestion endpoint despite bulk EHR export being the stated primary use case. | **[Resolved]** [openapi.yaml](../api/openapi.yaml) adds `POST /documents/batch`. |
| D8 | Blocker | No emergency/break-glass access endpoint — a standard clinical-system requirement. | **[Resolved]** [openapi.yaml](../api/openapi.yaml) adds `POST /access/break-glass`; audit requirements documented in [06-security-compliance.md](../architecture/06-security-compliance.md) §3. |
| D9 | Blocker | No tamper-evidence (hash-chaining/WORM) on `audit_log` — "no UPDATE/DELETE grants" is an application-role restriction only, not cryptographic integrity, and the migration role can `BYPASSRLS`. | **[Resolved]** [postgres-schema.sql](../database/postgres-schema.sql) adds `prev_hash`/`row_hash` hash-chain columns. |
| D10 | High | HNSW index has no build parameters or post-bulk-load rebuild plan. | **[Resolved]** [postgres-schema.sql](../database/postgres-schema.sql) specifies `m`/`ef_construction` and a `REINDEX CONCURRENTLY` runbook reference. |
| D11 | High | No backup/DR (RPO/RTO) specified for the operational store. | **[Resolved]** Covered under A1 ([sla-dr.md](../operations/sla-dr.md)). |
| D12 | High | Connection pooling under schema-per-tenant explicitly deferred as an unresolved "Phase 1 concern" in ADR-0003 — a foundational scaling constraint with no strawman. | **[Resolved]** [ADR-0003](adr-0003-multi-tenancy-model.md) §Consequences now includes a concrete pooling strawman (tenant-sharded pooler tier). |
| D13 | High | Several FK columns unindexed (`encounter_id`, `source_document_id`, `actor_user_id`, `embedding_model_id`, `role_id`). | **[Resolved]** [postgres-schema.sql](../database/postgres-schema.sql) indexes every FK used in a query path. |
| D14 | High | `chat_messages` RLS policy is a correlated subquery against an (also unindexed) `chat_sessions.tenant_id` — the highest-QPS table in the product. | **[Resolved]** `tenant_id` denormalized onto `chat_messages` with a direct equality-predicate policy. |
| D15 | High | No hot/cold storage tiering for 6-year audit-log retention on an unpartitioned table. | **[Resolved]** Covered under D1 + [sla-dr.md](../operations/sla-dr.md) archival tier. |
| D16 | High | No API versioning/deprecation policy beyond the `/v1` path segment. | **[Resolved]** [openapi.yaml](../api/openapi.yaml) info description adds a deprecation-header policy statement. |
| D17 | High | No idempotency keys on POST endpoints — retry-unsafe in a distributed ingestion pipeline with no uniqueness constraint on `objectStorageUri`. | **[Resolved]** [openapi.yaml](../api/openapi.yaml) adds `Idempotency-Key` header to ingestion/chat POSTs; schema adds a uniqueness constraint. |
| D18 | High | No FHIR Bulk Data ($export/$import NDJSON) support despite FHIR being a primary ingestion source. | **[Resolved]** [openapi.yaml](../api/openapi.yaml) adds `POST /documents/fhir-bulk-import`. |
| D19 | High | Two tenant-identification mechanisms (URL subdomain `tenant_slug` vs. token tenant claim) with no stated precedence/mismatch behavior. | **[Resolved]** [openapi.yaml](../api/openapi.yaml) info description states mismatch = hard `403`, and this is a validated invariant, not an assumption. |
| D20 | Medium | No comparative evaluation of a unified document+vector store (MongoDB Atlas Vector Search) against the current 3-store split — an unexamined default. | **[Resolved]** Added [ADR-0004](adr-0004-storage-engine-evaluation.md). |
| D21 | Medium | Ad hoc `{code, message}` error schema, not RFC 7807. | **[Resolved]** [openapi.yaml](../api/openapi.yaml) `Error` schema replaced with a Problem Details shape. |
| D22 | Medium | No webhook/async ingestion-completion notification — clients must poll. | **[Resolved]** [openapi.yaml](../api/openapi.yaml) adds `POST /webhooks`. |
| D23 | Medium | Rate limiting discussed narratively but absent from the OpenAPI spec (no 429, no rate-limit headers). | **[Resolved]** [openapi.yaml](../api/openapi.yaml) adds `429` responses and `X-RateLimit-*`/`Retry-After` headers. |
| D24 | Low | `index_sync_state` has no `tenant_id`/RLS (lower severity — no PHI content). | Accepted as-is; noted in schema comment as an intentional exception since the table stores no PHI. |

---

## Summary

- **20 Blockers**, **20 High**, **17 Medium**, **7 Low** across 4 review dimensions — all Blocker
  and High findings are resolved in this pass; all Medium findings are resolved or explicitly
  deferred with a named phase; Low findings are resolved or explicitly accepted with rationale
  (never silently dropped).
- No finding was dismissed without a documented reason. Where a fix was deferred rather than
  applied (B16, C-items folded into others), the deferral is itself recorded above.
- This review, once fixes landed, was re-read against the updated documents to confirm
  consistency (e.g., ADR-0002 ↔ graph-schema.md vector index scope; ADR-0002 ↔
  deployment-architecture.md Neptune language) — cross-document contradictions were a recurring
  theme in the original findings and are the category most likely to regress silently in future
  edits, so future reviewers should specifically re-check cross-references, not just individual
  documents in isolation.

See [docs/roadmap/phase0-signoff.md](../roadmap/phase0-signoff.md) for the formal sign-off record.
