# Graph Database Schema (Neo4j)

**Status:** Phase 0 — Design only
**Depends on:** [03-knowledge-graph.md](../architecture/03-knowledge-graph.md), [ADR-0002](../design/adr-0002-graph-database-choice.md)
**Revised after Phase 0 review:** see [phase-0-architecture-review.md](../design/phase-0-architecture-review.md) findings C1–C21.

Isolation: database-per-tenant (Neo4j multi-database, large/regulated accounts) or shared
database with `tenant_id` property + service-layer filtering (smaller accounts) — see
[ADR-0003](../design/adr-0003-multi-tenancy-model.md). The constraints/indexes below apply
identically in either mode; in shared-database mode every uniqueness constraint that references
a tenant-scoped entity is composite on `(tenant_id, ...)`.

## 0. Design patterns applied throughout this schema

Four patterns recur across the node/relationship definitions below and are stated once here
rather than repeated per entity:

- **Unmapped entities are first-class, not dropped.** Real extraction never achieves 100%
  ontology coverage (local lab panels, hospital-specific formulary codes, free-text symptom
  phrasing). Every entity type that links to a shared ontology concept also accepts a link to a
  `LocalConcept` node instead, with an explicit review workflow rather than silent loss.
- **Shared clinical *knowledge* lives on the ontology layer; patient-specific *facts* live on the
  instance layer.** Generic pharmacological/clinical knowledge (a drug treats a condition, two
  drugs are contraindicated together) is knowledge about the *concept*, true for every patient —
  it is modeled once between ontology-concept nodes, not duplicated per patient-instance node.
  Patient-specific facts (this patient was prescribed this drug on this date) reference the
  concept via `HAS_RXNORM_CONCEPT`-style edges rather than re-asserting the general knowledge.
- **Every clinically actionable relationship carries provenance and confidence**, not just the
  ones that happened to be modeled first — `{confidence, source_document_id, asserted_by,
  evidence_level}` is a standard property set applied consistently, per finding C2.
- **Facts are asserted, not overwritten.** Where two sources disagree about a patient fact
  (§1.4), both assertions are kept and reconciled by policy rather than one silently replacing
  the other.

## 1. Node labels & properties

```cypher
// --- Tenant-scoped patient-instance nodes ---
// schema_version tags the property model each node was written under, so a
// future migration can identify which nodes predate a schema change — Neo4j
// has no Flyway-equivalent, so this is the platform's own migration marker.

(:Patient {
  patient_id: STRING,        // matches platform.patients.patient_id (Postgres)
  tenant_id: STRING,
  deidentification_level: STRING,
  schema_version: INTEGER
})

(:Encounter {
  encounter_id: STRING,
  tenant_id: STRING,
  started_at: DATETIME,
  facility: STRING,
  schema_version: INTEGER
})

// Bi-temporal properties on every patient-fact node: valid_from/valid_to are
// CLINICAL time (when the fact was/is true of the patient); asserted_at is
// TRANSACTION time (when the platform recorded it). This is what makes
// "what did we know as of date X" queries answerable — see §1.3.
(:Condition {
  condition_id: STRING,
  tenant_id: STRING,
  display_text: STRING,
  clinical_status: STRING,
  valid_from: DATE,           // onset
  valid_to: DATE,              // abatement/resolution, null if ongoing
  asserted_at: DATETIME,
  schema_version: INTEGER
})

(:Medication {
  medication_id: STRING,
  tenant_id: STRING,
  display_text: STRING,
  dosage: STRING,
  status: STRING,              // active | discontinued | completed | entered-in-error
  valid_from: DATE,
  valid_to: DATE,
  asserted_at: DATETIME,
  schema_version: INTEGER
})

(:Procedure { procedure_id: STRING, tenant_id: STRING, display_text: STRING, performed_at: DATE, asserted_at: DATETIME, schema_version: INTEGER })
(:Observation { observation_id: STRING, tenant_id: STRING, display_text: STRING, value_numeric: FLOAT, unit: STRING, observed_at: DATETIME, asserted_at: DATETIME, schema_version: INTEGER })
(:AllergyIntolerance { allergy_id: STRING, tenant_id: STRING, display_text: STRING, asserted_at: DATETIME, schema_version: INTEGER })
(:Device { device_id: STRING, tenant_id: STRING, display_text: STRING, schema_version: INTEGER })
(:Provider { provider_id: STRING, tenant_id: STRING, display_name: STRING, schema_version: INTEGER })
(:Organization { org_id: STRING, tenant_id: STRING, name: STRING, schema_version: INTEGER })

// --- Unmapped/local concept fallback — see §0 and finding C1 ---
// Written whenever extraction cannot resolve a UMLS CUI. review_status tracks
// a terminology-team triage queue that can promote a LocalConcept to a real
// ontology link once mapped, without losing the fact in the interim.
(:LocalConcept {
  local_concept_id: STRING,
  tenant_id: STRING,
  source_system: STRING,
  local_code: STRING,
  display_text: STRING,
  review_status: STRING,       // unmapped | under_review | mapped | rejected
  proposed_cui: STRING,        // filled in once a terminology reviewer proposes a mapping, null until then
  schema_version: INTEGER
})

// --- Shared, tenant-agnostic reference nodes (read-only at query time) ---
// Reconciled across vocabularies via UMLS CUI. Versioned rather than mutated
// in place (see §1.2) so an already-linked patient fact never silently
// changes meaning under a terminology update.

(:UmlsConcept { cui: STRING, preferred_name: STRING, ontology_release: STRING, valid_from: DATE, superseded_by: STRING })
(:SnomedConcept { snomed_id: STRING, cui: STRING, preferred_term: STRING, ontology_release: STRING, valid_from: DATE, superseded_by: STRING })
(:IcdCode { icd_code: STRING, icd_version: STRING, cui: STRING, description: STRING, ontology_release: STRING })
(:LoincCode { loinc_code: STRING, cui: STRING, long_common_name: STRING, ontology_release: STRING })
(:RxNormConcept { rxcui: STRING, cui: STRING, name: STRING, tty: STRING, ontology_release: STRING, valid_from: DATE, superseded_by: STRING })
(:HpoTerm { hpo_id: STRING, cui: STRING, name: STRING, ontology_release: STRING })

// --- Shared clinical-knowledge nodes (tenant-agnostic reference content) ---
// Guideline/AdverseEvent definitions are reference knowledge, not per-patient
// occurrences — see §0 pattern 2 and finding C20. A tenant's patient-specific
// occurrence of an adverse event is still recorded on the Encounter/Observation
// instance layer via MENTIONS/HAS_OBSERVATION; this node is the shared
// definition those instances point back to.
(:ClinicalTrial { trial_id: STRING, nct_id: STRING, title: STRING, phase: STRING })
(:AdverseEventDefinition { ae_def_id: STRING, meddra_code: STRING, display_text: STRING, severity: STRING })
(:Guideline { guideline_id: STRING, title: STRING, publisher: STRING, version: STRING, effective_date: DATE })
(:LiteratureSource { source_id: STRING, doi: STRING, pmid: STRING, title: STRING })
(:DocumentChunk { chunk_id: STRING, tenant_id: STRING, document_id: STRING })  // mirrors Postgres document_chunks

// --- GraphRAG community structures (docs/architecture/03-knowledge-graph.md §1) ---

(:Community {
  community_id: STRING,
  tenant_id: STRING,
  level: INTEGER,             // hierarchy level from Leiden community detection
  leiden_run_id: STRING,      // identifies which clustering run produced this community — see §1.4 ID-stability note
  summary: STRING,            // null until summarized
  summary_embedding: LIST,    // populated alongside summary
  summarized_at: DATETIME,    // null = not yet summarized; queries MUST check this, not just absence of rows — see §4
  schema_version: INTEGER
})
```

## 2. Relationship types

```cypher
(:Patient)-[:HAS_ENCOUNTER]->(:Encounter)
(:Encounter)-[:DIAGNOSED_WITH]->(:Condition)
(:Encounter)-[:PRESCRIBED]->(:Medication)
(:Encounter)-[:PERFORMED]->(:Procedure)
(:Encounter)-[:HAS_OBSERVATION]->(:Observation)
(:Patient)-[:HAS_ALLERGY]->(:AllergyIntolerance)

// Generic clinical knowledge lives on the ONTOLOGY layer (RxNormConcept/
// SnomedConcept), not between patient-instance Medication/Condition nodes —
// see §0 pattern 2 and finding C10. A patient-specific contraindication
// ALERT is derived at query time by traversing from the patient's active
// Medication -> its RxNormConcept -> CONTRAINDICATED_WITH -> another
// RxNormConcept -> back to any of the patient's OTHER active medications,
// not by an edge stored directly between two Medication instances.
(:RxNormConcept)-[:TREATS {confidence: FLOAT, source_document_id: STRING, asserted_by: STRING, evidence_level: STRING}]->(:SnomedConcept)
(:RxNormConcept)-[:CONTRAINDICATED_WITH {confidence: FLOAT, source_document_id: STRING, asserted_by: STRING, evidence_level: STRING}]->(:RxNormConcept)
(:RxNormConcept)-[:CONTRAINDICATED_WITH {confidence: FLOAT, source_document_id: STRING, asserted_by: STRING, evidence_level: STRING}]->(:SnomedConcept)
(:RxNormConcept)-[:CAUSES {confidence: FLOAT, source_document_id: STRING, asserted_by: STRING, evidence_level: STRING}]->(:AdverseEventDefinition)
(:SnomedConcept)-[:INCREASES_RISK_OF {confidence: FLOAT, source_document_id: STRING, asserted_by: STRING, evidence_level: STRING}]->(:SnomedConcept)

(:Condition)-[:HAS_SNOMED_CONCEPT]->(:SnomedConcept)
(:AllergyIntolerance)-[:HAS_SNOMED_CONCEPT]->(:SnomedConcept)     // was promised in the entity table but missing from earlier relationship list — finding C14
(:Device)-[:HAS_SNOMED_CONCEPT]->(:SnomedConcept)                 // GUDID linkage modeled via the same edge type, device-specific identifiers as SnomedConcept.gudid_di property (not shown above for brevity)
(:SnomedConcept)-[:MAPPED_TO]->(:IcdCode)
(:SnomedConcept)-[:HAS_CUI]->(:UmlsConcept)
(:Medication)-[:HAS_RXNORM_CONCEPT]->(:RxNormConcept)
(:RxNormConcept)-[:HAS_CUI]->(:UmlsConcept)
(:Observation)-[:HAS_LOINC_CODE]->(:LoincCode)
(:LoincCode)-[:HAS_CUI]->(:UmlsConcept)

// Fallback path when no ontology concept resolves — see §0 pattern 1
(:Condition)-[:HAS_LOCAL_CONCEPT]->(:LocalConcept)
(:Medication)-[:HAS_LOCAL_CONCEPT]->(:LocalConcept)
(:Observation)-[:HAS_LOCAL_CONCEPT]->(:LocalConcept)

(:ClinicalTrial)-[:STUDIES]->(:RxNormConcept)
(:ClinicalTrial)-[:REPORTED {count: INTEGER, source_document_id: STRING, asserted_by: STRING}]->(:AdverseEventDefinition)
(:Guideline)-[:RECOMMENDS {strength: STRING, source_document_id: STRING, asserted_by: STRING}]->(:RxNormConcept)
(:Guideline)-[:CITES]->(:LiteratureSource)

(:DocumentChunk)-[:MENTIONS {char_offset_start: INTEGER, char_offset_end: INTEGER}]->(:Condition)
(:DocumentChunk)-[:MENTIONS]->(:Medication)
(:DocumentChunk)-[:MENTIONS]->(:Procedure)
(:DocumentChunk)-[:MENTIONS]->(:AdverseEventDefinition)

(:Condition)-[:MEMBER_OF]->(:Community)
(:Medication)-[:MEMBER_OF]->(:Community)
(:Community)-[:SUBCOMMUNITY_OF]->(:Community)

// Conflicting-assertion pattern — see §1.4. When two sources disagree about
// the same patient fact, the superseded assertion is kept and linked, not
// deleted, preserving the audit trail required by ADR-0001's traceability goal.
(:Medication)-[:SUPERSEDES {reason: STRING, resolved_at: DATETIME}]->(:Medication)
```

## 3. Entity resolution & conflict handling

**Concept-level dedup** (does this mention refer to an entity we already have a node for) uses
UMLS CUI as the reconciliation key, per [03-knowledge-graph.md §2.1](../architecture/03-knowledge-graph.md#21-core-clinical-entity-types).
This is necessary but not sufficient — it prevents duplicate *nodes* for the same concept, but
says nothing about what to do when two *facts* about a patient conflict.

**Fact-level conflict resolution** (what do we do when Note A says a medication was discontinued
and Note B, from a different encounter or source, says it's active) is a distinct policy,
applied whenever a new patient-instance node/edge would contradict an existing one:

1. Both assertions are written — the newer one as a fresh node, linked to the prior one via
   `SUPERSEDES {reason, resolved_at}`. Neither is deleted or silently overwritten.
2. Precedence for "what is currently true" queries: **most recent `asserted_at` (transaction
   time) wins**, unless the earlier assertion has a strictly higher `evidence_level` (e.g., a
   structured pharmacy-system FHIR feed outranks an NLP-extracted mention from unstructured
   text) — that exception is itself recorded in the `SUPERSEDES.reason` property, not applied
   silently.
3. Point-in-time queries ("what did we believe as of date X") ignore precedence entirely and
   return whichever assertion had the latest `asserted_at <= X` — this is exactly what the
   bi-temporal properties in §1 exist to support, and is the query class ADR-0001 identifies as
   a compliance requirement.

## 4. Local vs. global search

| Mode | Trigger | Mechanism | Cost |
|---|---|---|---|
| **Local search** | Entity-anchored query ("what is drug X contraindicated with?") | Match entry entity → walk 1–2 hop neighborhood (hop-bounded, see §6) → combine with linked document chunks | Low, real-time |
| **Global search** | Thematic/corpus-wide query ("emerging safety signals this quarter") | Map-reduce over pre-computed community summaries at the relevant hierarchy level | Higher, batched/opt-in — see §5 |
| **DRIFT-style blended** | Ambiguous scope | Start at community level to locate relevant region, drift into local entity search for detail | Medium |

A global-search query against a tenant/level with no summarized communities yet must distinguish
**"not yet summarized"** from **"summarized, genuinely no results"** — returning an empty list
for both is indistinguishable to the routing layer in
[02-rag-hybrid-retrieval.md §2](../architecture/02-rag-hybrid-retrieval.md#2-query-time-retrieval)
and would silently look like a successful-but-empty search. See the corrected query in §7.

## 5. Community summarization trigger & SLA

Per [ADR-0001](../design/adr-0001-hybrid-graph-vector-retrieval.md), community summarization is
opt-in rather than run eagerly for every tenant. The opt-in mechanism and SLA:

- **Trigger:** a tenant admin enables `deep_analytics_enabled = true` on the tenant record (API:
  `PATCH /admin/tenants/{tenantId}` — see [openapi.yaml](../api/openapi.yaml)), or the platform
  auto-enables it for tenants whose usage pattern shows repeated global-search-shaped queries
  falling back to local search (a signal logged by the router per
  [02-rag-hybrid-retrieval.md §2.1](../architecture/02-rag-hybrid-retrieval.md#21-query-routing)).
- **SLA:** first summarization run completes within 24 hours of opt-in (nightly batch window);
  subsequent re-summarization follows the re-clustering cadence in §6 below. This SLA is
  surfaced to the tenant admin in the UI when they opt in, not left implicit.

## 6. Leiden re-clustering cadence & community ID stability

Leiden community detection is **non-deterministic across independent runs** — re-running it
naively can reassign entities to differently-numbered communities, silently invalidating every
existing `MEMBER_OF` edge, community summary, and any citation that references a `community_id`.

- **Re-clustering trigger:** scheduled nightly for tenants with `deep_analytics_enabled`, plus an
  event-driven trigger when cumulative graph mutations since the last run exceed 5% of total
  node count (large ingestion batches shouldn't wait for the nightly window).
- **ID stability:** each run is tagged with a `leiden_run_id`. New communities are matched
  against the previous run's communities by Jaccard similarity of member-entity sets; a match
  above a fixed threshold carries the prior `community_id` and summary forward
  (re-summarization only if membership changed materially), rather than issuing a new ID and
  discarding the old summary. Communities with no adequate match are treated as genuinely new and
  queued for summarization per the SLA in §5.

## 7. Constraints & indexes

```cypher
// Uniqueness — composite on tenant_id for every tenant-scoped label, not just three
CREATE CONSTRAINT patient_id_unique IF NOT EXISTS FOR (p:Patient) REQUIRE (p.tenant_id, p.patient_id) IS UNIQUE;
CREATE CONSTRAINT encounter_id_unique IF NOT EXISTS FOR (e:Encounter) REQUIRE (e.tenant_id, e.encounter_id) IS UNIQUE;
CREATE CONSTRAINT condition_id_unique IF NOT EXISTS FOR (c:Condition) REQUIRE (c.tenant_id, c.condition_id) IS UNIQUE;
CREATE CONSTRAINT medication_id_unique IF NOT EXISTS FOR (m:Medication) REQUIRE (m.tenant_id, m.medication_id) IS UNIQUE;
CREATE CONSTRAINT procedure_id_unique IF NOT EXISTS FOR (p:Procedure) REQUIRE (p.tenant_id, p.procedure_id) IS UNIQUE;
CREATE CONSTRAINT observation_id_unique IF NOT EXISTS FOR (o:Observation) REQUIRE (o.tenant_id, o.observation_id) IS UNIQUE;
CREATE CONSTRAINT allergy_id_unique IF NOT EXISTS FOR (a:AllergyIntolerance) REQUIRE (a.tenant_id, a.allergy_id) IS UNIQUE;
CREATE CONSTRAINT device_id_unique IF NOT EXISTS FOR (d:Device) REQUIRE (d.tenant_id, d.device_id) IS UNIQUE;
CREATE CONSTRAINT provider_id_unique IF NOT EXISTS FOR (p:Provider) REQUIRE (p.tenant_id, p.provider_id) IS UNIQUE;
CREATE CONSTRAINT org_id_unique IF NOT EXISTS FOR (o:Organization) REQUIRE (o.tenant_id, o.org_id) IS UNIQUE;
CREATE CONSTRAINT local_concept_id_unique IF NOT EXISTS FOR (l:LocalConcept) REQUIRE (l.tenant_id, l.local_concept_id) IS UNIQUE;
CREATE CONSTRAINT chunk_id_unique IF NOT EXISTS FOR (d:DocumentChunk) REQUIRE (d.tenant_id, d.chunk_id) IS UNIQUE;
CREATE CONSTRAINT community_id_unique IF NOT EXISTS FOR (c:Community) REQUIRE (c.tenant_id, c.community_id) IS UNIQUE;

// Shared reference/knowledge nodes are tenant-agnostic — global uniqueness
CREATE CONSTRAINT umls_cui_unique IF NOT EXISTS FOR (u:UmlsConcept) REQUIRE u.cui IS UNIQUE;
CREATE CONSTRAINT snomed_id_unique IF NOT EXISTS FOR (s:SnomedConcept) REQUIRE s.snomed_id IS UNIQUE;
CREATE CONSTRAINT rxnorm_id_unique IF NOT EXISTS FOR (r:RxNormConcept) REQUIRE r.rxcui IS UNIQUE;
CREATE CONSTRAINT loinc_code_unique IF NOT EXISTS FOR (l:LoincCode) REQUIRE l.loinc_code IS UNIQUE;
CREATE CONSTRAINT trial_id_unique IF NOT EXISTS FOR (t:ClinicalTrial) REQUIRE t.trial_id IS UNIQUE;
CREATE CONSTRAINT guideline_id_unique IF NOT EXISTS FOR (g:Guideline) REQUIRE g.guideline_id IS UNIQUE;

// Vector index — COMMUNITY-LEVEL ONLY. ADR-0002 describes this as scoped to
// "entity/community description embeddings," but no per-entity embedding
// property is defined anywhere in this schema; the scope is community
// summaries only (finding C16 — ADR-0002 language corrected to match this).
// Bulk narrative-text vector search stays in pgvector per ADR-0001/ADR-0002.
CREATE VECTOR INDEX community_summary_embedding IF NOT EXISTS
  FOR (c:Community) ON (c.summary_embedding)
  OPTIONS { indexConfig: { `vector.dimensions`: 1536, `vector.similarity_function`: 'cosine' } };

// Full-text index supporting hybrid graph+fulltext queries. NOTE: Neo4j
// full-text queries cannot apply an inline tenant_id predicate — every caller
// MUST post-filter results by tenant_id in application code (see §8 query
// example) in shared-database mode. This is a mandatory pattern, not optional
// hardening — a missed post-filter here is a cross-tenant PHI leak (finding C9).
CREATE FULLTEXT INDEX entity_display_text IF NOT EXISTS
  FOR (n:Condition|Medication|Procedure|AdverseEventDefinition) ON EACH [n.display_text];
```

## 8. Example queries (illustrative, Phase 1 implementation reference)

**Local search** — entity neighborhood expansion, using the full-text index (not an unindexed
`CONTAINS` scan — finding C8) with mandatory tenant post-filter (finding C9) and a bounded hop
count (finding C11):

```cypher
CALL db.index.fulltext.queryNodes('entity_display_text', $drug_name_query) YIELD node, score
WHERE node:Medication AND node.tenant_id = $tenant_id
WITH node AS m, score
ORDER BY score DESC
LIMIT 10
MATCH (m)-[:HAS_RXNORM_CONCEPT]->(rx:RxNormConcept)
OPTIONAL MATCH (rx)-[:CAUSES]->(ae:AdverseEventDefinition)
OPTIONAL MATCH (rx)-[:CONTRAINDICATED_WITH]->(other:RxNormConcept)
RETURN m, collect(DISTINCT ae) AS adverse_events, collect(DISTINCT other) AS contraindicated_concepts
LIMIT 25;
```

**Patient-specific contraindication check** — traversal from a patient's active medications
through the shared ontology layer (§0 pattern 2), bounded to 2 hops:

```cypher
MATCH (p:Patient {tenant_id: $tenant_id, patient_id: $patient_id})-[:HAS_ENCOUNTER]->(:Encounter)-[:PRESCRIBED]->(active:Medication)
WHERE active.status = 'active'
MATCH (active)-[:HAS_RXNORM_CONCEPT]->(rx1:RxNormConcept)
MATCH path = (rx1)-[:CONTRAINDICATED_WITH*1..1]-(rx2:RxNormConcept)<-[:HAS_RXNORM_CONCEPT]-(other:Medication)
WHERE other.tenant_id = $tenant_id AND other.status = 'active' AND other <> active
RETURN active, other, path
LIMIT 50;
```

**Global search** — community summary map-reduce seed query, distinguishing "not yet
summarized" from "no results" (finding C5):

```cypher
MATCH (c:Community {tenant_id: $tenant_id, level: $level})
WITH count(c) AS total, count(CASE WHEN c.summarized_at IS NOT NULL THEN 1 END) AS summarized
RETURN
  CASE WHEN total = 0 THEN 'no_communities'
       WHEN summarized = 0 THEN 'not_yet_summarized'
       ELSE 'ready' END AS status,
  total, summarized;
// Application code branches on `status`: 'not_yet_summarized' triggers the
// opt-in prompt / falls back to local search per §4, rather than returning an
// empty result set indistinguishable from a genuine zero-match query.
```

## 9. Related documents

- [Knowledge Graph Design](../architecture/03-knowledge-graph.md)
- [ADR-0002: Neo4j as graph store](../design/adr-0002-graph-database-choice.md)
- [ADR-0001: Hybrid retrieval rationale](../design/adr-0001-hybrid-graph-vector-retrieval.md)
- [Phase 0 Architecture Review — findings C1-C21](../design/phase-0-architecture-review.md)
