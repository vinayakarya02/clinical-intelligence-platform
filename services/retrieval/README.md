# Retrieval Service — Hybrid RAG Intelligence Layer

Phase 2 of the Clinical Intelligence Platform. Turns the chunks the ingestion service
persisted into answers-in-waiting: embeddings, a vector index, a clinical knowledge graph,
three retrieval strategies fused into one ranking, a reranker, a token-bounded cited
context, and the prompt that carries it.

**Scope boundary:** this service stops at a rendered prompt. It never calls a language
model, holds no conversation state, and exposes no chat surface — those are Phase 2's
conversational layer and beyond
([implementation-roadmap.md](../../docs/roadmap/implementation-roadmap.md)).

## Pipeline

```
  question ──► ┌────────────────────────────────────────────────────┐
               │ 1. route       intent → strategy weights            │
               ├────────────────────────────────────────────────────┤
               │ 2. retrieve    vector ∥ keyword ∥ graph             │
               │                (concurrent, failure-isolated)       │
               ├────────────────────────────────────────────────────┤
               │ 3. fuse        weighted Reciprocal Rank Fusion      │
               ├────────────────────────────────────────────────────┤
               │ 4. rerank      7 interpretable relevance features   │
               ├────────────────────────────────────────────────────┤
               │ 5. assemble    dedup, token budget, citations,      │
               │                graph evidence, retrieval trace      │
               ├────────────────────────────────────────────────────┤
               │ 6. prompt      versioned templates from a registry  │
               └────────────────────────────────────────────────────┘
                                       │
                          evidence? ───┴─── no evidence?
                       answer_question      no_evidence prompt
```

Four decisions carry the design.

**Retrievers run concurrently and fail independently.** Total latency is the slowest
strategy rather than their sum, and an outage in one store degrades the answer instead of
erasing it. Which strategies degraded is returned to the caller, not swallowed — an answer
assembled without the graph is a different answer.

**Fusion combines ranks, not scores.** A cosine similarity, a BM25 score, and a graph
traversal confidence are not on a common scale and cannot be added. RRF only needs the
ordering each strategy produced, so no normalisation constant has to be tuned or maintained.

**Routing widens when it is unsure.** Confidence comes from the *margin* between the top two
intents, and below a threshold the broad default bundle is dispatched. Guessing narrowly and
being wrong costs the answer entirely; guessing broadly costs latency.

**Tenant scoping is checked twice.** Every store filters by tenant, and the pipeline
re-checks every fused candidate before assembly. The second check is not redundancy for its
own sake: a store-level filter that regresses leaks PHI *silently*, and this is the one point
every candidate from every strategy passes through. Mismatches are dropped, logged at
`error`, and counted in the trace, where a non-zero count always means a bug in a store
filter.

**An empty context is a structural stop, not a hint.** When nothing survives retrieval the
pipeline renders the `no_evidence` template and never produces an answerable prompt. A model
handed an empty context answers from parametric memory, and no post-hoc grounding check can
catch that — there was never any context to check against.

**Retrieved evidence is data, never instructions.** Clinical documents are uploaded by users
and scanned from third parties, so a passage can contain text addressed to the model. The
system prompt says so explicitly and the task prompt delimits the evidence region. Hostile
text is quoted inside the markers rather than stripped — suppressing it would hide a poisoned
document from the clinician reading the answer.

## Modules

| Module | Responsibility |
|---|---|
| `domain.py` | `RetrievalCandidate` and the query/trace types every stage passes along |
| `embeddings/` | Provider protocol, batching, retry, cache, model-version keys |
| `vectorstore/` | `VectorStore` protocol; MongoDB Atlas and exact in-memory backends |
| `graph/` | Node/relationship models, ontology labels, provenance, Neo4j + in-memory stores, traversal |
| `retrievers/` | Vector, BM25 keyword, and graph retrievers behind one `Retriever` protocol |
| `fusion.py` | Weighted Reciprocal Rank Fusion |
| `routing.py` | Rule-based intent classification and per-intent fusion weights |
| `reranking.py` | Feature reranker; `Reranker` protocol a cross-encoder implements later |
| `context.py` | Token budgeting, content dedup, citation ordering, graph evidence rendering |
| `prompts/` | Versioned prompt registry loaded from YAML |
| `evaluation/` | Retrieval metrics, grounding metrics, and the eval harness |
| `pipeline.py` | Orchestration and the no-evidence gate |
| `demo.py` | Runnable end-to-end verification, benchmarks, and evaluation |

## Running it

```bash
python -m cip_retrieval.demo
```

Indexes two clinical documents, builds a small drug/condition graph, answers three question
shapes, prints every stage's decision, scores a labelled eval set, and benchmarks the chain.
This is the Phase 2 verification run; see
[phase-2-engineering-report.md](../../docs/design/phase-2-engineering-report.md) for the
recorded results.

## What is deliberately not a real implementation yet

**`HashingEmbeddingProvider` is a baseline, not a clinical model.** It produces genuine
lexical similarity — two paraphrases of the same hyperkalemia finding score 0.52 cosine, the
same passage against a chest-CT impression scores 0.02 — which is enough to make the whole
pipeline runnable, testable, and evaluable without an inference endpoint. It has no clinical semantics: it will not connect "MI" to "myocardial infarction".
The `EmbeddingProvider` protocol is the seam a real model plugs into, and the model key is
carried on every stored vector so a migration is a re-index against a new key rather than a
silent mixing of incomparable vector spaces.

**`FeatureReranker` is a linear scorer, not a cross-encoder.** Phase 0 specifies a
cross-encoder; that needs a model this phase does not have. The feature reranker is the
baseline it must beat, and it has one property a cross-encoder does not: every score
decomposes into named features, so "why was this ranked first" is answerable.

**The in-memory vector and graph stores are not mocks.** They implement the same filter,
threshold, and traversal semantics as Atlas and Neo4j — the in-memory vector store is
*exact* rather than approximate, so a recall assertion that fails against it is a real bug
rather than ANN noise. `Settings` refuses them in deployed environments.
