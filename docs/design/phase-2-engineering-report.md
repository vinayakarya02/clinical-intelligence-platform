# Phase 2 Engineering Report — Retrieval, Embeddings, Knowledge Graph, Hybrid RAG

**Scope delivered:** the intelligence layer between persisted chunks and a language model —
embedding pipeline, vector store, knowledge-graph engine, three retrievers, rank fusion,
query routing, reranking, context assembly, prompt orchestration, and an evaluation
framework. No conversational UI, no model invocation; those remain out of scope by
instruction.

**Verification status:** 604 of 615 tests pass and 11 skip — the skips are integration tests
requiring live PostgreSQL/MongoDB/Neo4j and OCR tests requiring a Tesseract binary, neither
of which is available here. `ruff format`, `ruff check`, and `pyright` are clean across the
repository. The end-to-end run (`python -m cip_retrieval.demo`) exercises the full chain and
scores a labelled eval set.

| | |
|---|---|
| Retrieval service source | 34 modules, 5,666 lines |
| Retrieval tests | 215 tests |
| Repository total | 615 tests — 604 pass, 11 skip |

---

## 1. Architecture decisions

### 1.1 Everything crosses a protocol boundary, and only where a second implementation is real

`typing.Protocol` seams exist for `EmbeddingProvider`, `VectorStore`, `GraphStore`,
`Retriever`, `Reranker`, `EmbeddingCache`, and `TokenEstimator`. Most have two working
implementations today rather than a hypothetical future one:

| Protocol | Implementations today |
|---|---|
| `VectorStore` | `MongoAtlasVectorStore`, `InMemoryVectorStore` |
| `GraphStore` | `Neo4jGraphStore`, `InMemoryGraphStore` |
| `Retriever` | `VectorRetriever`, `KeywordRetriever`, `GraphRetriever` |
| `EmbeddingCache` | `InMemoryEmbeddingCache`, `NullEmbeddingCache` |
| `TokenEstimator` | `HeuristicTokenEstimator` *(one)* |
| `EmbeddingProvider` | `HashingEmbeddingProvider` *(one)* |
| `Reranker` | `FeatureReranker` *(one)* |

The three single-implementation protocols are deliberate, not speculative. `EmbeddingProvider`
is the abstraction the phase instructions mandate explicitly ("never hardcode a specific
embedding model"), and it earns its place regardless: the model key
`provider/model/dimensions` is stored on every vector and every cache entry, so swapping
models is a re-index against a new key rather than a silent mixing of incomparable vector
spaces. `Reranker` and `TokenEstimator` each have a known, scheduled second implementation —
a cross-encoder and a real tokenizer — and both currently sit behind a seam whose output
already feeds the evaluation harness.

### 1.2 Fusion combines ranks, not scores

Reciprocal Rank Fusion (`k=60`) over each strategy's ordering. Cosine similarity, BM25, and
graph traversal confidence are not on a common scale; adding them requires normalisation
constants that must be re-tuned every time a retriever changes. RRF needs only the ordering,
so the fusion layer has no tuning surface that can silently go stale. Weights per strategy
are applied to the reciprocal-rank contribution, which is where routing expresses preference.

Duplicate candidates from different strategies merge additively, retaining every per-strategy
score and rank. That is what makes the trace explicable after the fact: a candidate that
placed 12th by vector and 1st by keyword is visibly a keyword win.

### 1.3 Routing is rules, and low confidence widens rather than narrows

Clinical question shapes are a small stable set with strong lexical markers. A rule set is
inspectable, testable, and free; an LLM classifier would add a network call and a failure
mode to every query. Confidence is derived from the *margin* between the top two intents, so
a query matching two intents equally is correctly reported as ambiguous however many markers
it hit. Below the threshold, the broad default bundle is dispatched.

All three strategies run for every intent. Weighting, not exclusion, expresses preference:
skipping a strategy means a mis-classified question loses its only good source, and because
the retrievers run concurrently, running all three costs the slowest one rather than the sum.

### 1.4 Tenant isolation is pushed into the index, not applied afterwards

`tenant_id` and `model_key` go in the `filter` clause of Atlas's `$vectorSearch`, never in a
later `$match`. Atlas applies that filter *inside* the index while traversing candidates.
Post-filtering the returned top-K is not equivalent: if another tenant's documents crowd this
tenant's out of the ANN candidate pool, post-filtering returns empty for a query that should
have matched. This is a correctness property, not an optimisation, and it is asserted
directly against the built pipeline so it can be tested without an Atlas cluster.

`index_definition()` lives beside the query builder for a related reason: Atlas *silently
ignores* filters on fields not declared as `filter` type in the index. An omission there is a
tenant-isolation failure that looks like working code.

### 1.5 Ontology concepts are shared; patient facts are tenant-scoped

`GraphNode` rejects a `tenant_id` on ontology labels (RxNorm, SNOMED, LOINC, ICD). Duplicating
the ontology per tenant would defeat the shared concept layer that makes cross-tenant
knowledge reuse possible. The model enforces it rather than documenting it, and the
end-to-end demo hit that check during development — which is the intended behaviour.

Clinically actionable relationships (`CONTRAINDICATED_WITH`, `CAUSES`, `TREATS`, …) are
rejected at construction without provenance. An actionable edge with no attributable source
cannot be reviewed, defended, or safely surfaced to a clinician, and enforcing it in the
model covers every write path rather than every write site.

### 1.6 Context assembly is a hard budget with content-based deduplication

The token budget reserves answer space up front. Overflowing the window means the provider
truncates from the end with no knowledge of what it is cutting — the citation an answer
depends on can vanish while the answer still sounds confident.

Deduplication is by normalised content, not id: chunk overlap means adjacent chunks
legitimately share text, and re-ingested documents produce different ids for identical
content. Citations are numbered in presentation order, so `[1]` is the first block the model
sees; any other ordering makes the model's own numbering disagree with the citation list,
which reads as a hallucinated citation during review even when retrieval was correct.

Graph evidence is rendered as attributed assertions with confidence and evidence level, never
as prose. A graph edge is an inference, and presenting it as a quotation would let the model
treat it as something a clinician wrote.

### 1.7 Prompts are versioned data, not string literals

Five templates in YAML behind a registry, seven versions in total — a superseded version is
retained rather than edited in place, so an answer produced under it stays reproducible. Required variables are
validated against the template body at load time, so a template referencing a variable the
code never supplies fails at startup rather than rendering a prompt with a hole in it. Every
rendered prompt reports the template versions it used, which is what makes an answer-quality
regression traceable to a prompt change.

---

## 2. Trade-offs accepted

**A hashing embedding provider instead of a clinical model.** No inference endpoint or model
weights are available in this environment. Rather than stub the provider and leave the
pipeline undemonstrable, `HashingEmbeddingProvider` produces genuine lexical similarity
(signed feature hashing over character n-grams, sublinear TF, L2-normalised): two
paraphrases of the same hyperkalemia finding score 0.52 cosine, while the same passage
against a chest-CT impression scores 0.02. That is enough to make retrieval,
fusion, reranking, and evaluation real and measurable. It has *no clinical semantics* — it
will not connect "MI" to "myocardial infarction". It is the baseline a real model must beat,
and the model-key mechanism makes the swap a re-index rather than a rewrite.

**A feature reranker instead of a cross-encoder.** Same cause. The seven features (fusion
agreement, lexical overlap, section affinity, graph support, clinical confidence, source
quality, freshness) are transparent and tunable against the harness. A cross-encoder will
outrank it; the harness is how that claim gets tested rather than assumed.

**In-memory stores alongside the production backends.** Atlas Vector Search is an Atlas-only
feature; without a local implementation, retrieval could not run on a developer machine or in
CI, and every retrieval bug would surface only in a cloud environment. The in-memory store is
*exact* rather than approximate, making it stricter than production for test purposes. Cost:
O(n) per query, viable for development corpora only — `Settings` refuses it in deployed
environments.

**ADR-0004 was superseded, not quietly contradicted.** Phase 0 rejected MongoDB Atlas for the
vector tier in favour of PostgreSQL + pgvector. The Phase 2 instruction directs Atlas.
[ADR-0007](adr-0007-vector-store-mongodb-atlas.md) records the reversal, the premise that
genuinely changed (MongoDB was already adopted in Phase 1 for parsed artifacts), and the
consequence that remains unresolved: Atlas is a managed cloud service, and the on-prem
deployment path for data-residency-constrained hospital tenants now needs a different vector
backend. That gap is real and is recorded as technical debt below, not papered over.

**Six evaluation cases, not sixty.** The eval set is labelled by section over a two-document
corpus. It is enough to make the harness runnable end to end and to catch a ranking
regression in CI — and it did catch two. It is nowhere near a production eval set, which
Phase 0 specifies as curated and clinician-reviewed.

---

## 3. Benchmarks

In-process, five runs, Windows 11 / Python 3.11, 15 chunks from 2 documents, structured debug
logging enabled.

| Stage | Time | Notes |
|---|---|---|
| document → chunk | 4.9 ms | 15 chunks from 2 documents |
| chunk → embedding | 10.7 ms | 15 vectors, 384-dim |
| per-chunk embedding | 0.71 ms | hashing provider |
| graph construction | 0.13 ms | 5 nodes, 4 relationships |
| retrieval end-to-end (p50) | 8.2 ms | route → fuse → rerank → assemble → prompt |
| retrieval end-to-end (p95) | 9.3 ms | |
| peak memory | 0.54 MB | pipeline build + all queries |

Re-measured after the review fixes. The added ACL re-check and relevance floor are two
linear passes over the fused candidates and cost ~0.1 ms at p50; retrieval quality is
unchanged on every metric below.

Per-stage split of one retrieval (entity-relationship query): route 0.23 ms, retrieve 4.81 ms
*(three strategies concurrently)*, fuse 0.65 ms, rerank 1.46 ms, assemble 0.38 ms, prompt
1.54 ms. Retrieval dominates, as it should — it is the only stage doing store work.

**These numbers bound the pipeline's own cost and say nothing about production latency.**
The in-memory stores do no network I/O — Atlas and Neo4j round-trips will dominate — and a
real embedding model costs orders of magnitude more than the hashing baseline. The Phase 0
latency budget in [02-rag-hybrid-retrieval.md](../architecture/02-rag-hybrid-retrieval.md) §5
remains unvalidated against real infrastructure.

### Retrieval quality (6-case labelled set)

| Metric | Before fixes | After fixes |
|---|---|---|
| precision@1 | 0.667 | **1.000** |
| MRR | 0.750 | **1.000** |
| NDCG@1 | 0.667 | **1.000** |
| NDCG@3 | 0.772 | **0.912** |
| recall@3 | 0.833 | **0.889** |
| hit_rate@10 | 0.833 | **1.000** |
| average_precision | 0.750 | **0.889** |
| context_recall | 0.667 | **0.722** |
| intent_accuracy | 1.000 | 1.000 |
| graph_coverage | 1.000 | 1.000 |

`context_precision` is 0.19 and is *not* a defect: the context builder packs five blocks
against a budget that comfortably fits them, so most blocks are unlabelled-but-plausible
context rather than the single labelled answer. It becomes meaningful when the budget is
tight enough to force choices.

---

## 4. Bugs found and fixed

Every one of these was found by running the pipeline end to end, not by unit tests.

### 4.1 Graph retrieval was completely dead

`GraphRetriever` called `find_nodes(text=query.text)`, and the in-memory store checked whether
the *whole question* appeared inside a node's display name. That is entity linking backwards:
"Does lisinopril interact with spironolactone?" never appears inside "Lisinopril", so
multi-word queries matched nothing, ever.

Worse, it hid a divergence: Neo4j used a full-text index (token semantics) while the
in-memory store used substring containment, so local behaviour did not predict production
behaviour. Fixed with token-overlap matching in both, bringing the two stores to the same
semantics. The interaction query now returns 4 graph candidates and 3 graph edges.

### 4.2 Query routing scored repeated markers only once

Several related markers share one regex alternation, and the code tested whether the pattern
fired rather than how many distinct markers matched. "What are the emerging safety signals
across trials?" — three thematic markers — scored 2.5, exactly tying a single weak
definitional marker, and lost the tie on dictionary order. Fixed by counting distinct matched
markers, capped at two per signal so a query repeating synonyms cannot let one signal
outweigh every other intent.

### 4.3 Radiology reports had no sectioned findings *(Phase 1 defect, found in Phase 2)*

The clinical section vocabulary had no entries for `FINDINGS`, `TECHNIQUE`, `COMPARISON`, or
`INDICATION`. Radiology notes are a supported document type, so the entire findings body of
every radiology report — the clinically load-bearing part — fell through to
`document_preamble`. That silently broke section filters, the reranker's section affinity,
and citation headings alike. Fixed by adding the four canonical sections; `IMPRESSION` was
already correctly mapped to `assessment`.

### 4.4 "…on admission" hijacked lab-value questions

The reranker's section affinity treated a bare `admission` or `hospital` marker as evidence
for the hospital-course section. But "…on admission" is overwhelmingly a *temporal anchor* on
a value question. For "What was the potassium level on admission?", the hospital course's
narrative mention ("Potassium trended down after spironolactone was held") outranked the lab
table holding the actual value of 5.4 mmol/L. Fixed by requiring multi-word course markers
and adding an imaging affinity for the newly-detectable `findings` section.

The same patterns had a second latent bug: `\bcourse|admission|hospital` anchors `\b` to the
first branch only, so the rest could match mid-word. Every alternation is now grouped.

### 4.5 Verification-harness defects (found and fixed during the phase)

- The demo invented a `GraphWriter`/`NodeSpec` API that does not exist — rewritten against
  the real models.
- The demo put `tenant_id` on ontology nodes; the model correctly rejected it (§1.5).
- Two context-builder tests used identical fixture text, which the (correct) content
  deduplication absorbed before the assertions could hold.

Each fix carries a regression test: token-overlap entity matching, distinct-marker routing,
radiology section detection, the admission-anchor ranking, and the imaging affinity.

---

## 4A. Adversarial production-readiness review

A second pass over every subsystem, conducted against the finished code rather than
alongside writing it. Two Blockers and six High findings; all are fixed and each carries a
regression test.

**The single most useful observation is structural: every one of the isolation defects lived
in the Cypher backend, which had no test double at all.** The in-memory store enforced the
rules correctly, so CI was green while production would have leaked. A recording Neo4j
manager (`tests/retrieval/test_graph.py::_RecordingManager`) now asserts the generated
Cypher directly, the same way `TestAtlasPipeline` asserts the `$vectorSearch` pipeline. That
gap — "the implementation we actually deploy is the one we cannot test" — was worth more
than any individual bug it was hiding.

### Blockers

**B1 — Neo4j relationship writes matched endpoints without tenant scoping.**
`MATCH (a:Patient {key: row.start_key})` matches *every* tenant's node with that key, because
a patient key is unique only within a tenant. `MERGE` then attached one tenant's clinical
assertion to another tenant's patient — a cross-tenant PHI **write**. Endpoint matches are
now scoped by `tenant_id` for patient-scoped labels, derived from `is_patient_scoped()`
rather than sampled from the first node in the batch.

**B2 — Relationship `tenant_id` was never persisted, and `neighbours()` never filtered on it.**
`GraphRelationship.tenant_id` was dropped on write. The in-memory store filtered edges by
tenant; Neo4j could not, because the value was not there. A tenant-scoped assertion drawn
between two *shared* ontology nodes has two null-tenant endpoints, so endpoint-only
filtering exposed it to every tenant. The tenant is now part of the edge's MERGE key and of
the `neighbours()` predicate.

### High

**H1 — The post-retrieval ACL re-check specified in §2.2 was never implemented.**
`TenantContext.require_tenant` documented itself as existing for it, and
`RetrievalTrace.filtered_by_acl` existed to record it, but nothing called either. A
regression in any single store filter would have leaked silently. The pipeline now re-checks
every fused candidate at the one point they all pass through, drops mismatches, logs at
`error`, and records the count.

**H2 — `min_score` was honoured by the vector retriever alone.** Worse than incomplete:
because fusion consumes *ranks*, unfiltered weak keyword and graph hits were promoted into
the positions the filtered vector hits vacated, so setting a relevance floor actively
degraded ranking. The floor is now applied uniformly after fusion and recorded in
`filtered_by_threshold`, which strengthens the no-evidence gate that depends on it.

**H3 — Graph evidence escaped the context budget.** Its tokens were counted *after* packing
and added to the reported total, never checked against it. Blocks filled the budget, then up
to `max_graph_evidence` lines were appended for free and the provider truncated the tail —
dropping exactly the citations an answer depends on, which is the failure the module's own
docstring claims it prevents. Evidence is now charged to the same budget as passages.

**H4 — The embedding cache key ignored `InputKind`.** Asymmetric models encode a query and a
passage into different vectors for the same string; keying on content alone served the
passage vector for an identical query, silently substituting the wrong encoding. The key is
now `model_key + input_kind + sha256(text)`.

**H5 — Raw user text was passed to the Neo4j full-text index as a Lucene query.**
`db.index.fulltext.queryNodes` takes a query *string*, not a literal. Ordinary clinical
questions — `Na+/K+ ratio`, `CT (chest)`, an unbalanced quote — raise a Lucene parse error,
which the pipeline's failure isolation then reports as a degraded strategy rather than an
error. Silent, and reproducible only in production. Text is now reduced to OR-joined
alphanumeric tokens, which removes every reserved character by construction and matches the
in-memory store's semantics.

**H6 — No defence against indirect prompt injection.** Evidence text is interpolated
verbatim, and clinical documents are uploaded by users and scanned from third parties. A
poisoned passage instructing the model to call a drug pair safe had nothing standing against
it. `clinical_system` v002 states that retrieved evidence is data and never instructions;
`answer_question` v002 delimits the evidence region. Hostile text is quoted inside the
markers rather than stripped — suppressing it would hide a poisoned document from the
clinician reading the answer. Both prior versions are retained, per the templates' own
editing rules, which also gives the registry's version-selection path its first real
exercise.

### Medium (fixed)

- **M1** — In-memory `find_nodes` returned the first `limit` matches in insertion order while
  Neo4j ordered by full-text score, so the local store anchored traversal on different
  entities than production. Now ranked by token overlap.
- **M2** — `merged_with` did not check `tenant_id`. Fusion merges by id; two stores returning
  the same id for different tenants would have blended them into one candidate carrying the
  first tenant's identity. Now refused.
- **M3** — A query whose tokens are all shorter than three characters ("is it ok?") matched
  *every* node and returned arbitrary traversal entry points. Supplied-but-unusable search
  text is now a miss rather than an unfiltered scan.
- **M4** — `InMemoryEmbeddingCache` documented its 50,000-entry default as ~150 MB. Measured:
  ~12.4 KB per 384-dimension entry, so ~592 MB — Python boxes every float. An operator sizing
  a container from that comment gets OOM-killed. Corrected, and the default lowered to 10,000
  (~125 MB) with batch ingest expected to raise it deliberately.
- **M5** — Prompt version selection is a lexicographic `max`, so an unpadded `v9` would
  outrank `v10` and silently serve a superseded prompt. The format is now enforced at load.
- **M6** — Re-indexing a BM25 document id under a different tenant left the id in the previous
  tenant's set, where it resolved through `_documents` to the replacement — handing the old
  tenant another tenant's text.

### Accepted, not fixed

- **`GraphRetriever._find_entry_points` issues one store call per entry label** — seven
  sequential round-trips to Neo4j per graph retrieval. Real latency debt, but the fix changes
  the `GraphStore` protocol to accept multiple labels, which is a wider change than this
  review should make. Recorded in §6.
- **Fusion double-counts a candidate appearing twice in one strategy's list.** No current
  retriever can produce that, and guarding it would add a dedup pass to the hot path for a
  condition the retrievers already exclude.
- **`dropped_over_budget` conflates block-count and token-budget drops.** Cosmetic; both are
  "did not fit".

---

## 5. Remaining limitations

1. **No clinical embedding model.** The hashing provider has no clinical semantics. Retrieval
   quality figures above measure the *pipeline*, not the *system a clinician would use*.
2. **No cross-encoder reranker.** Phase 0 specifies one; the feature reranker is the baseline.
3. **Nothing has run against real Atlas, Neo4j, or PostgreSQL.** Integration tests exist and
   are gated behind `CIP_RUN_INTEGRATION=1`; no cluster was available.
4. **Six eval cases over two documents.** Not a clinical eval set; a smoke test with metrics.
5. **No community detection.** GraphRAG global search (Leiden communities, community
   summaries) is Phase 2's graph-analytics half and is not implemented — only local
   entity-anchored traversal is.
6. **No entity extraction from documents.** The graph is populated by explicit writes. The
   NER/ontology-linking stage that would build it from ingested text does not exist, so graph
   coverage in production would currently be whatever a separate process wrote.
7. **Latency budget unvalidated.** See §3.
8. **Grounding metrics are computed but not enforced.** `evaluation/grounding.py` implements
   faithfulness, groundedness, and exact numeric consistency checks; nothing yet blocks an
   answer that fails them, because nothing yet generates answers.

## 6. Technical debt

| Item | Why it matters | Suggested phase |
|---|---|---|
| On-prem vector backend | ADR-0007 chose a managed cloud service; data-residency-constrained hospital tenants have no path | Phase 4 (on-prem packaging) |
| Entity extraction → graph | Without it the knowledge graph is only as complete as manual writes | Phase 2 remainder |
| Community detection + global search | "Themes across the corpus" questions route to `thematic` but have no global-search backend to serve them | Phase 2 remainder / Phase 4 |
| Eval set curation | Every ranking claim rests on 6 cases | Before any model bake-off |
| Reranker weights are hand-set | Defaults are reasoned, not fitted; the harness exists to fit them | After eval-set curation |
| `_SECTION_AFFINITY` is a hand-maintained table | Two of the five bugs above were in it; it will keep drifting from the section vocabulary | Consider deriving from `CLINICAL_SECTION_PATTERNS` |
| Graph entry-point lookup is one query per label | Seven sequential Neo4j round-trips per graph retrieval; needs a multi-label `find_nodes` | Next graph work |
| Cypher backend has no integration coverage | The recording double asserts the *query*; only a live Neo4j proves it *runs* | Before any deployment |
| BM25 index is in-process | Fine for the current scale; a real deployment needs OpenSearch or the Atlas Search text index | Phase 3 |

---

## 7. Production readiness

**Ready:** the architecture, the failure isolation, the tracing, and the test discipline.
Tenant scoping is enforced at construction (`VectorQuery` cannot be built unscoped), pushed
into the index, and — since the review — re-checked independently after retrieval. Retriever
failures degrade rather than black out, and degradation is reported. Every retrieval produces
a trace with per-strategy counts, weights, stage timings, and now the ACL and threshold
rejection counts. The no-evidence gate is structural and the relevance floor that feeds it is
applied uniformly.

**Not ready:** the intelligence, and the Cypher backend. A hashing embedder and a linear
reranker are honest placeholders. More seriously, the review found that both cross-tenant
defects lived in the one component with no test double — the Neo4j path is now asserted at
the query level, but *no code in this service has ever executed against a live Neo4j, Atlas,
or PostgreSQL instance*. The recording double proves the Cypher we generate is correct; it
cannot prove it runs.

**Assessment:** Phase 2 delivers a production-shaped retrieval engine with placeholder
intelligence. Most of the remaining work is substitution — a clinical embedding model, a
cross-encoder, real backends, a curated eval set — rather than redesign, and the seams that
make those substitutions cheap are each backed by two working implementations.

The review changed how much weight that assessment carries. Before it, the isolation model
was described as sound; two Blockers say it was sound only in the implementation that never
ships. The lesson generalises past the individual bugs: **wherever there are two
implementations of a protocol and only one is tested, the tested one is telling you what you
want to hear.** The Atlas backend is in the same position the Neo4j backend was — its
`$vectorSearch` pipeline is asserted, never executed.

Two risks now lead, in order:

1. **Nothing has run against real infrastructure.** Integration tests exist and are gated on
   `CIP_RUN_INTEGRATION=1`; standing up Atlas, Neo4j, and PostgreSQL and running them is the
   highest-value next action, ahead of any model work.
2. **All quality claims rest on six eval cases over two documents.** Model selection without
   a trustworthy eval set is guesswork with extra steps.
