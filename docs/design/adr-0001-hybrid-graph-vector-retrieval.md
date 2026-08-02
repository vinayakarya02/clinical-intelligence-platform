# ADR-0001: Hybrid Graph + Vector + Keyword Retrieval over Vector-Only RAG

**Status:** Accepted (Phase 0)
**Date:** 2026-08-01

## Context

A naive RAG implementation (chunk → embed → cosine-similarity retrieve → generate) is
insufficient for clinical use because:

- Clinical questions are frequently **multi-hop** ("what adverse events are associated with
  drugs that treat condition X in patients also taking drug Y?") — pure vector similarity does
  not reason over relationships.
- Clinical terminology relies on **exact-match signals** (drug names, ICD/SNOMED codes, lab
  codes) that dense embeddings alone under-weight relative to sparse/keyword matching.
- Provenance and **multi-hop traceability** (which guideline, which trial, which patient fact
  led to this answer) is a compliance requirement, not a nice-to-have — a flat vector index
  loses this structure.
- Published benchmarks (see research notes) show hybrid dense+sparse retrieval beating dense-only
  by 26–31%, and graph-augmented retrieval reducing hallucination substantially on multi-hop
  clinical queries versus vector-only RAG (CliCARE, KG2RAG, GNN-RAG).

## Decision

Retrieval is served by three coordinated subsystems, fused at query time rather than one subsystem
approximating all three:

1. **Dense vector search** (pgvector) — semantic/paraphrase matching over chunked unstructured text.
2. **Sparse keyword search** (OpenSearch/BM25) — exact term, code, and drug-name matching.
3. **Graph traversal** (Neo4j) — multi-hop entity relationships, ontology-linked reasoning, and
   community-level thematic summaries (GraphRAG-style local/global search).

A routing layer classifies each query (entity-heavy/multi-hop → graph-first; broad/thematic →
community summary map-reduce; ambiguous → vector-first then graph-expand) and fuses ranked
results via Reciprocal Rank Fusion before reranking. Full design in
[02-rag-hybrid-retrieval.md](../architecture/02-rag-hybrid-retrieval.md) and
[03-knowledge-graph.md](../architecture/03-knowledge-graph.md).

## Consequences

- **Positive:** higher precision on multi-hop clinical reasoning; native support for
  citation/provenance chains; exact-match reliability on codes/drug names that clinicians expect.
- **Negative:** three retrieval subsystems instead of one materially increases operational
  surface area, ingestion pipeline complexity (dual-write to graph + vector + relational store),
  and requires a fusion/routing layer that itself needs evaluation and tuning.
- **Mitigation:** ingestion is unified behind one event-driven pipeline (§1 of
  [02-rag-hybrid-retrieval.md](../architecture/02-rag-hybrid-retrieval.md)) so the three stores
  are always built from the same source event, not three independent pipelines that can drift.

## Alternatives considered

- **Vector-only RAG** — rejected: insufficient multi-hop reasoning and provenance for clinical/
  regulatory use, per research cited above.
- **Graph-only (no vector index)** — rejected: unstructured narrative text (clinical notes,
  literature) doesn't reduce cleanly to graph triples without heavy information loss; vector
  search remains necessary for narrative recall.
- **Full Microsoft GraphRAG (LLM-summarized communities at index time for all data)** — deferred:
  expensive upfront indexing cost is justified for corpus-wide thematic queries (pharmacovigilance
  signal detection) but not for every tenant's full corpus on day one. Phase 1 scopes full
  GraphRAG community summarization to opt-in "deep analytics" corpora; local graph search and
  hybrid vector+keyword ship first. See LazyGraphRAG-style deferred summarization as the
  implementation pattern.
