# ADR-0006: Structural Chunking in Phase 1, Embedding-Based Chunking in Phase 2

**Status:** Accepted (Phase 1)
**Date:** 2026-08-02
**Refines:** [02-rag-hybrid-retrieval.md §1.2](../architecture/02-rag-hybrid-retrieval.md#12-chunking-strategy)

## Context

Phase 0 specifies semantic chunking by embedding-similarity breakpoint detection: embed
each sentence, cut a chunk boundary where cosine distance between consecutive sentence
embeddings exceeds a tuned percentile.

Phase 1 excludes embeddings entirely. The specified algorithm therefore cannot be
implemented in this phase — not as a matter of effort, but because its central input does
not exist yet. Three options were available:

1. Implement embedding-similarity chunking anyway, pulling embeddings into Phase 1.
2. Ship fixed-size chunking now and replace it in Phase 2.
3. Ship a different chunking strategy that uses signals available today, behind an
   interface the Phase 0 algorithm can later implement.

## Decision

**Option 3.** Chunking sits behind the `Chunker` protocol
(`cip_ingestion.processing.chunking`). Phase 1 implements
`StructuralSemanticChunker`; Phase 2 adds `EmbeddingSemanticChunker` as a second
implementation of the same protocol, selected by configuration.

The Phase 1 chunker uses the structure the document already carries — clinical section
boundaries, table blocks, paragraph breaks, sentence boundaries — as its semantic signal.
For clinical documents this is a strong proxy rather than a weak substitute: sections like
"MEDICATIONS" and "HOSPITAL COURSE" are topical *by construction*, which is exactly the
boundary embedding-similarity detection tries to recover statistically. A discharge
summary's real topic shifts are already labelled in the document.

Token counting is deferred the same way, and for the same reason. The authoritative
tokenizer belongs to the embedding model, and that model is chosen by the Phase 1 bake-off
([02-rag-hybrid-retrieval.md §1.3](../architecture/02-rag-hybrid-retrieval.md#13-embedding-model)).
`TokenEstimator` is a protocol with a calibrated heuristic implementation; the real
tokenizer drops in without touching the chunker. The heuristic deliberately
*over*-estimates dense clinical text — overshooting produces chunks slightly under the
model's limit, while undershooting produces chunks that get silently truncated at
embedding time.

Rejected alternatives:

- **Option 1 (pull embeddings into Phase 1)** — would import the embedding model choice,
  the BAA/PHI question for embedding API calls, and vector storage into a phase scoped to
  exclude them. Phase 1's value is proving the ingestion and compliance path; widening it
  delays that.
- **Option 2 (fixed-size now)** — the cheapest option and the worst. Fixed-size chunking
  splits medication lists mid-table and separates a lab value from its reference range,
  and every document ingested before the replacement would need reprocessing. Structural
  chunking produces chunks that are already correct at section granularity.

## Consequences

- **Positive:** Phase 1 produces genuinely usable chunks — section-aligned, table-intact,
  offset-accurate — rather than placeholders awaiting replacement. Phase 2 changes a
  configuration value and adds a class.
- **Negative:** within a long section of uniform prose, structural chunking falls back to
  sentence-boundary packing and will not detect a *sub-section* topic shift that
  embedding-similarity would. This affects long narrative sections (a multi-paragraph
  "HOSPITAL COURSE") more than the structured sections that dominate clinical documents.
- **Negative:** chunk boundaries will change when Phase 2 switches strategies, so the
  corpus must be re-chunked. This is anticipated: `ingestion_runs.pipeline_version` exists
  precisely so "reprocess everything below version X" is an indexed query rather than a
  guess, and the parsed artifact in MongoDB ([ADR-0005](adr-0005-phase1-service-decomposition.md))
  makes re-chunking cheap by avoiding re-OCR.
- **Measurable at the switch:** the Phase 1 chunker becomes the baseline the eval set
  ([02-rag-hybrid-retrieval.md §4](../architecture/02-rag-hybrid-retrieval.md#4-evaluation--observability))
  scores Phase 2's chunker against. If embedding-similarity chunking does not beat
  structural chunking on clinical documents, that is a result worth having rather than an
  assumption worth shipping.

## Invariants both implementations must hold

Enforced by tests in `tests/unit/test_chunking.py`, and binding on any future `Chunker`:

1. Chunks never cross a section boundary.
2. Table blocks are never split.
3. Chunks never exceed `chunk_max_tokens`, except a single indivisible unit that exceeds
   it alone — which is emitted whole rather than truncated.
4. Character ranges are exact, so every chunk traces back to its source span for citation.
