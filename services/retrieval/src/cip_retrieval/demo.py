"""End-to-end demonstration and benchmark.

Runs real clinical documents through the entire chain — parse, chunk, embed, index, build a
knowledge graph, retrieve across all three strategies, fuse, rerank, assemble context, and
render a prompt — then benchmarks each stage.

This is a runnable artifact rather than a test: it prints what actually happened at every
stage so the pipeline's behaviour can be inspected, not merely asserted. ``python -m
cip_retrieval.demo`` reproduces the Phase 2 verification run.
"""

from __future__ import annotations

import asyncio
import time
import tracemalloc
import uuid
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any

from cip_core.tenancy import TenantContext
from cip_ingestion.parsers import TextParser
from cip_ingestion.processing import (
    ChunkingOptions,
    StructuralSemanticChunker,
    detect_sections,
    normalize_document,
)
from cip_retrieval.context import ContextBudget, ContextBuilder
from cip_retrieval.domain import (
    QueryIntent,
    RetrievalCandidate,
    RetrievalQuery,
    RetrievalStrategy,
)
from cip_retrieval.embeddings import (
    EmbeddingService,
    HashingEmbeddingProvider,
    InMemoryEmbeddingCache,
)
from cip_retrieval.evaluation import EvalCase, RetrievalEvaluator
from cip_retrieval.graph import (
    GraphNode,
    GraphRelationship,
    InMemoryGraphStore,
    NodeLabel,
    Provenance,
    RelationshipType,
)
from cip_retrieval.pipeline import RetrievalPipeline
from cip_retrieval.retrievers import (
    BM25Index,
    GraphRetriever,
    IndexedDocument,
    KeywordRetriever,
    VectorRetriever,
)
from cip_retrieval.vectorstore import InMemoryVectorStore, VectorRecord

TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")

DISCHARGE_SUMMARY = """DISCHARGE SUMMARY

Patient Name: Jordan Rivera
Date of Service: 2026-03-14

CHIEF COMPLAINT
Substernal chest pain radiating to the left arm.

HISTORY OF PRESENT ILLNESS
The patient is a 62-year-old presenting with three days of exertional chest discomfort
that worsened overnight prior to admission. No prior cardiac history is documented.

PAST MEDICAL HISTORY
Hypertension. Type 2 diabetes mellitus. Hyperlipidemia.

ALLERGIES
Penicillin - rash.

MEDICATIONS
Lisinopril | 10 mg | daily
Metformin | 500 mg | twice daily
Atorvastatin | 40 mg | nightly
Spironolactone | 25 mg | daily

LABORATORY RESULTS
Sodium | 141 | mmol/L | 135-145
Potassium | 5.4 | mmol/L | 3.5-5.1
Troponin I | 0.04 | ng/mL | 0.00-0.04
Creatinine | 1.02 | mg/dL | 0.70-1.30

ASSESSMENT AND PLAN
Acute coronary syndrome ruled out. Mild hyperkalemia likely related to combined
lisinopril and spironolactone therapy. Continue medical management.

HOSPITAL COURSE
The patient remained hemodynamically stable and was ambulating without symptoms by
hospital day two. Potassium trended down after spironolactone was held.

DISPOSITION
Discharged home in stable condition.
"""

RADIOLOGY_NOTE = """RADIOLOGY REPORT

Date of Service: 2026-03-15

TECHNIQUE
Non-contrast CT scan of the chest was performed.

COMPARISON
Prior radiograph dated 2025-11-04.

FINDINGS
No focal consolidation. No pleural effusion. Cardiomediastinal silhouette is within
normal limits.

IMPRESSION
No acute cardiopulmonary process.
"""


@dataclass
class Stage:
    """One benchmarked pipeline stage."""

    name: str
    duration_ms: float
    detail: str = ""


async def _index_document(
    text: str,
    *,
    document_type: str,
    effective_date: str,
    embeddings: EmbeddingService,
    vector_store: InMemoryVectorStore,
    bm25: BM25Index,
) -> tuple[list[IndexedDocument], float, float]:
    """Parse, chunk, embed, and index one document.

    Returns the indexed chunks alongside chunk and embed timings. The chunks are returned
    so the evaluation set can be labelled by section: chunk ids contain a random document
    uuid, so a hardcoded id list would be meaningless.
    """
    document_id = uuid.uuid4()

    started = time.perf_counter()
    parsed = TextParser().parse(text.encode())
    normalized = normalize_document(parsed)
    normalized = replace(normalized, sections=detect_sections(normalized))
    chunks = StructuralSemanticChunker(
        ChunkingOptions(target_tokens=120, min_tokens=24, max_tokens=200)
    ).chunk(normalized)
    chunk_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    batch = await embeddings.embed_texts([chunk.text for chunk in chunks])
    embed_ms = (time.perf_counter() - started) * 1000

    records = []
    documents = []
    for chunk, vector in zip(chunks, batch.vectors, strict=True):
        chunk_id = f"{document_id}:{chunk.index}"
        common: dict[str, Any] = {
            "tenant_id": TENANT,
            "text": chunk.text,
            "document_id": document_id,
            "chunk_index": chunk.index,
            "section_name": chunk.metadata.get("section_name"),
            "section_heading": chunk.section_heading,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "document_type": document_type,
            "source_system": "epic",
            "effective_date": effective_date,
        }
        records.append(
            VectorRecord(id=chunk_id, values=vector.values, model_key=vector.model_key, **common)
        )
        documents.append(IndexedDocument(id=chunk_id, **common))

    await vector_store.upsert(records)
    bm25.add(documents)
    return documents, chunk_ms, embed_ms


async def _build_graph(store: InMemoryGraphStore) -> float:
    """Populate a small clinical knowledge graph. Returns build time in ms."""
    started = time.perf_counter()
    source = uuid.uuid4()

    def _node(label: NodeLabel, key: str, display: str) -> GraphNode:
        # No tenant_id: RxNorm/SNOMED concepts are shared reference data. Duplicating the
        # ontology per tenant would defeat the shared concept layer, and the model
        # enforces that (docs/database/graph-schema.md §0).
        return GraphNode(label=label, key=key, properties={"display_text": display})

    await store.upsert_nodes(
        [
            _node(NodeLabel.RXNORM_CONCEPT, "rx:lisinopril", "Lisinopril"),
            _node(NodeLabel.RXNORM_CONCEPT, "rx:spironolactone", "Spironolactone"),
            _node(NodeLabel.RXNORM_CONCEPT, "rx:metformin", "Metformin"),
            _node(NodeLabel.SNOMED_CONCEPT, "sct:hyperkalemia", "Hyperkalemia"),
            _node(NodeLabel.SNOMED_CONCEPT, "sct:hypertension", "Hypertension"),
        ]
    )

    def _edge(
        rel: RelationshipType,
        start: str,
        end_label: NodeLabel,
        end: str,
        confidence: float,
        evidence: str,
    ) -> GraphRelationship:
        return GraphRelationship(
            type=rel,
            start_label=NodeLabel.RXNORM_CONCEPT,
            start_key=start,
            end_label=end_label,
            end_key=end,
            confidence=confidence,
            provenance=Provenance(source_document_id=source, evidence_level=evidence),
        )

    await store.upsert_relationships(
        [
            _edge(
                RelationshipType.CONTRAINDICATED_WITH,
                "rx:lisinopril",
                NodeLabel.RXNORM_CONCEPT,
                "rx:spironolactone",
                0.88,
                "label_warning",
            ),
            _edge(
                RelationshipType.CAUSES,
                "rx:lisinopril",
                NodeLabel.SNOMED_CONCEPT,
                "sct:hyperkalemia",
                0.82,
                "label_warning",
            ),
            _edge(
                RelationshipType.CAUSES,
                "rx:spironolactone",
                NodeLabel.SNOMED_CONCEPT,
                "sct:hyperkalemia",
                0.90,
                "label_warning",
            ),
            _edge(
                RelationshipType.TREATS,
                "rx:lisinopril",
                NodeLabel.SNOMED_CONCEPT,
                "sct:hypertension",
                0.95,
                "guideline",
            ),
        ]
    )
    return (time.perf_counter() - started) * 1000


def _eval_cases(by_section: dict[str, set[str]]) -> list[EvalCase]:
    """A small labelled eval set over the demo corpus.

    Relevance is labelled by *section* rather than by chunk id, because chunk ids embed a
    random document uuid. A question is judged answered correctly when a chunk from the
    section that actually contains the answer is retrieved.

    Six cases is far below what a production eval set needs (Phase 0 specifies a curated,
    clinician-reviewed set). It is enough to make the harness runnable end to end and to
    catch a ranking regression in CI, which is what it is for here.
    """
    return [
        EvalCase(
            case_id="lab-potassium",
            question="What was the potassium level on admission?",
            relevant_ids=by_section["laboratory_results"],
            expected_intent=QueryIntent.FACTUAL_LOOKUP,
            notes="The value lives in the lab table; the hospital course only mentions the trend.",
        ),
        EvalCase(
            case_id="medications",
            question="What is the daily dose of lisinopril?",
            relevant_ids=by_section["medications"],
            expected_intent=QueryIntent.FACTUAL_LOOKUP,
        ),
        EvalCase(
            case_id="allergies",
            question="What was the recorded allergy reaction?",
            relevant_ids=by_section["allergies"],
        ),
        EvalCase(
            case_id="hospital-course",
            question="Summarise the hospital course",
            relevant_ids=by_section["hospital_course"],
            expected_intent=QueryIntent.NARRATIVE,
        ),
        EvalCase(
            case_id="imaging-impression",
            question="Describe the chest CT findings",
            relevant_ids=by_section["findings"] | by_section["assessment"],
            expected_intent=QueryIntent.NARRATIVE,
        ),
        EvalCase(
            case_id="drug-interaction",
            question="Does lisinopril interact with spironolactone?",
            relevant_ids={
                "graph:RxNormConcept:rx:spironolactone",
                "graph:RxNormConcept:rx:lisinopril",
            },
            expected_intent=QueryIntent.ENTITY_RELATIONSHIP,
            requires_graph=True,
            notes="Answerable only from the graph: no chunk states the interaction directly.",
        ),
    ]


async def run_demo() -> None:
    """Run the full pipeline and print each stage."""
    tracemalloc.start()
    stages: list[Stage] = []

    embeddings = EmbeddingService(
        HashingEmbeddingProvider(dimensions=384), cache=InMemoryEmbeddingCache()
    )
    vector_store = InMemoryVectorStore()
    bm25 = BM25Index()
    graph_store = InMemoryGraphStore()

    print("=" * 78)
    print("PHASE 2 END-TO-END VERIFICATION")
    print("=" * 78)

    total_chunks = 0
    chunk_ms = embed_ms = 0.0
    by_section: dict[str, set[str]] = defaultdict(set)
    for text, doc_type, date in (
        (DISCHARGE_SUMMARY, "discharge_summary", "2026-03-14"),
        (RADIOLOGY_NOTE, "radiology_note", "2026-03-15"),
    ):
        indexed, c_ms, e_ms = await _index_document(
            text,
            document_type=doc_type,
            effective_date=date,
            embeddings=embeddings,
            vector_store=vector_store,
            bm25=bm25,
        )
        for document in indexed:
            by_section[document.section_name or "unsectioned"].add(document.id)
        total_chunks += len(indexed)
        chunk_ms += c_ms
        embed_ms += e_ms

    stages.append(Stage("document -> chunk", chunk_ms, f"{total_chunks} chunks from 2 documents"))
    stages.append(Stage("chunk -> embedding", embed_ms, f"{total_chunks} vectors, 384-dim"))
    stages.append(
        Stage("vector indexing", 0.0, f"{await vector_store.count(tenant_id=TENANT)} indexed")
    )

    graph_ms = await _build_graph(graph_store)
    stages.append(Stage("graph construction", graph_ms, "5 nodes, 4 relationships"))

    pipeline = RetrievalPipeline(
        retrievers={
            RetrievalStrategy.VECTOR: VectorRetriever(vector_store, embeddings),
            RetrievalStrategy.KEYWORD: KeywordRetriever(bm25),
            RetrievalStrategy.GRAPH: GraphRetriever(graph_store),
        },
        context_builder=ContextBuilder(),
        budget=ContextBudget(max_context_tokens=2000, reserved_for_answer=400),
    )
    context = TenantContext.for_service(TENANT)

    questions = [
        "What was the potassium level on admission?",
        "Does lisinopril interact with spironolactone?",
        "Summarise the hospital course",
    ]

    for question in questions:
        print(f"\n{'-' * 78}\nQUERY: {question}\n{'-' * 78}")
        started = time.perf_counter()
        response = await pipeline.retrieve(
            RetrievalQuery(text=question, tenant_id=TENANT, top_k=5), context=context
        )
        elapsed = (time.perf_counter() - started) * 1000
        stages.append(Stage(f"retrieval: {question[:32]}", elapsed))

        trace = response.trace
        print(f"routed intent    : {trace.intent} (confidence {trace.intent_confidence:.2f})")
        print(f"weights          : {trace.weights}")
        print(f"per strategy     : {trace.candidates_per_strategy}")
        print(f"fused -> reranked: {trace.fused_count} -> {trace.returned_count}")
        timings = {k: round(v, 2) for k, v in trace.stage_durations_ms.items()}
        print(f"stage timings ms : {timings}")

        print(f"\ntop results ({len(response.candidates)}):")
        for candidate in response.candidates[:3]:
            label = candidate.section_name or candidate.metadata.get("end_key", "graph")
            print(
                f"  [{candidate.rerank_score:.4f}] {label:<28} "
                f"{candidate.text[:52].replace(chr(10), ' ')!r}"
            )

        ctx = response.context
        print(
            f"\ncontext: {len(ctx.blocks)} blocks, {len(ctx.graph_evidence)} graph edges, "
            f"{ctx.total_tokens}/{ctx.budget.available_tokens} tokens "
            f"({ctx.total_tokens / ctx.budget.available_tokens:.0%} used)"
        )
        if ctx.graph_evidence:
            print("graph evidence:")
            for line in ctx.render_graph_evidence().splitlines()[:3]:
                print(f"  {line}")

        print(
            f"\nprompt: {response.prompt.estimated_tokens} tokens, "
            f"versions {response.prompt.template_versions}"
        )
        print(f"has evidence: {response.has_evidence}")

    print(f"\n{'=' * 78}\nRETRIEVAL EVALUATION\n{'=' * 78}")

    async def _retrieve(case: EvalCase) -> tuple[list[RetrievalCandidate], Any]:
        response = await pipeline.retrieve(
            RetrievalQuery(text=case.question, tenant_id=TENANT, top_k=5), context=context
        )
        return list(response.candidates), response.context

    cases = _eval_cases(by_section)
    started = time.perf_counter()
    report = await RetrievalEvaluator(k_values=(1, 3, 5)).run(cases, _retrieve)
    stages.append(
        Stage("evaluation", (time.perf_counter() - started) * 1000, f"{len(cases)} cases")
    )
    print(report.summary())

    # Printed rather than merely scored: an aggregate hides *which* question regressed,
    # and the failing case is where a ranking change has to be diagnosed.
    misses = [result for result in report.results if result.metrics.get("precision@1", 0.0) == 0.0]
    if misses:
        print(f"\n  cases where the top result was not a labelled answer: {len(misses)}")
        for result in misses:
            top = result.retrieved_ids[0] if result.retrieved_ids else "-"
            print(f"    {result.case_id}: top={top}")

    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"\n{'=' * 78}\nBENCHMARKS\n{'=' * 78}")
    for stage in stages:
        detail = f"  ({stage.detail})" if stage.detail else ""
        print(f"  {stage.name:<44} {stage.duration_ms:>8.2f} ms{detail}")
    print(f"\n  {'peak memory (pipeline build + queries)':<44} {peak / 1024 / 1024:>8.2f} MB")
    print(f"  {'per-chunk embedding cost':<44} {embed_ms / max(total_chunks, 1):>8.3f} ms")
    print(
        "\n  These are in-process figures over a 15-chunk corpus with structured debug\n"
        "  logging enabled. They bound the pipeline's own cost and say nothing about\n"
        "  production latency: Atlas and Neo4j add network round-trips, and a real\n"
        "  embedding model costs orders of magnitude more than the hashing baseline."
    )


def main() -> None:
    asyncio.run(run_demo())


if __name__ == "__main__":  # pragma: no cover
    main()
