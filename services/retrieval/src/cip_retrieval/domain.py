"""Domain value objects for the retrieval engine.

Every stage — retrieval, fusion, reranking, context assembly — consumes and produces these
types rather than dictionaries, for the same reason the ingestion pipeline does: each stage
becomes independently testable, and a fusion test can construct candidates directly without
a vector store or a graph.

The type that carries the most weight is :class:`RetrievalCandidate`. It is deliberately
*additive*: each stage attaches its contribution (a rank, a fused score, a rerank score, a
provenance note) without discarding what earlier stages recorded. That is what makes the
retrieval trace in :class:`RetrievalTrace` reconstructable after the fact — "why did this
chunk surface for this query" is the first question asked when retrieval misbehaves, and it
is unanswerable if intermediate scores are overwritten.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

__all__ = [
    "GraphEvidence",
    "QueryIntent",
    "RetrievalCandidate",
    "RetrievalQuery",
    "RetrievalStrategy",
    "RetrievalTrace",
    "SourceKind",
    "content_digest_text",
]


def content_digest_text(text: str) -> str:
    """Stable digest of normalised text, used for content-based deduplication.

    Whitespace is collapsed and case folded before hashing so two chunks that differ only
    in formatting — which chunk overlap and re-ingestion both produce — deduplicate
    against each other. An id-based check would miss both cases.
    """
    import hashlib
    import re

    normalised = re.sub(r"\s+", " ", text).strip().casefold()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


class SourceKind(StrEnum):
    """Where a candidate came from.

    Kept explicit rather than inferred, because downstream consumers treat them
    differently: a generated summary chunk is retrieval scaffolding and must never be cited
    as a source of truth (docs/architecture/02-rag-hybrid-retrieval.md §1.2), and graph
    evidence is a relationship rather than a passage.
    """

    DOCUMENT_CHUNK = "document_chunk"
    GRAPH_ENTITY = "graph_entity"
    GRAPH_PATH = "graph_path"
    STRUCTURED_FACT = "structured_fact"


class QueryIntent(StrEnum):
    """Query shapes the router recognises.

    A bounded set, not open-ended classification: clinical questions cluster into a small
    number of shapes in practice, and a fixed enum is what lets routing be tested and its
    accuracy measured. ``UNKNOWN`` is a first-class value — the router dispatches the broad
    default bundle rather than guessing narrowly when confidence is low.
    """

    FACTUAL_LOOKUP = "factual_lookup"
    """Numeric or coded value retrieval: "what was the troponin", "sodium on admission"."""

    ENTITY_RELATIONSHIP = "entity_relationship"
    """Multi-hop relational: interactions, contraindications, what-treats-what."""

    DEFINITIONAL = "definitional"
    """Conceptual or explanatory: "what is X", "explain Y"."""

    NARRATIVE = "narrative"
    """Open prose over clinical documentation: "summarise the hospital course"."""

    THEMATIC = "thematic"
    """Corpus-wide, requires community summaries: "emerging safety signals this quarter"."""

    UNKNOWN = "unknown"


class RetrievalStrategy(StrEnum):
    """Which retriever produced (or should produce) a result."""

    VECTOR = "vector"
    KEYWORD = "keyword"
    GRAPH = "graph"


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """A retrieval request, scoped to a tenant.

    ``patient_id`` narrows retrieval to one patient's records. It is a filter, not a hint:
    a patient-scoped question that retrieved another patient's chunks would be a PHI
    incident, so it is pushed into every store's filter rather than applied afterwards.
    """

    text: str
    tenant_id: uuid.UUID
    patient_id: uuid.UUID | None = None
    top_k: int = 10
    intent: QueryIntent | None = None
    """Caller-declared intent. Overrides routing when set — an integration that knows what
    it is asking should not be second-guessed by a classifier."""
    filters: dict[str, Any] = field(default_factory=dict)
    min_score: float | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("Retrieval query text must not be empty")
        if self.top_k < 1:
            raise ValueError("top_k must be >= 1")


@dataclass(frozen=True, slots=True)
class GraphEvidence:
    """A relationship recovered from the knowledge graph.

    Carries its own provenance because a graph assertion is only as trustworthy as the
    document it came from — the requirement in
    docs/database/graph-schema.md §0 that every clinically actionable edge be attributable.
    """

    subject: str
    predicate: str
    object: str
    confidence: float = 1.0
    evidence_level: str | None = None
    source_document_id: uuid.UUID | None = None
    hops: int = 1

    def as_sentence(self) -> str:
        """Render the edge as readable text for context assembly."""
        return f"{self.subject} {self.predicate.replace('_', ' ').lower()} {self.object}"


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    """One retrieved item, accumulating scores as it moves through the pipeline.

    ``scores`` is a per-strategy map rather than a single float so fusion can weigh the
    strategies that found this item and reranking can explain itself. Overwriting a single
    score field would make the trace useless.
    """

    id: str
    """Stable identifier: a chunk id, or an entity/path key for graph results."""

    text: str
    source_kind: SourceKind
    tenant_id: uuid.UUID

    document_id: uuid.UUID | None = None
    chunk_index: int | None = None
    section_name: str | None = None
    section_heading: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    effective_date: dt.date | None = None
    document_type: str | None = None
    source_system: str | None = None

    scores: dict[str, float] = field(default_factory=dict)
    """Raw per-strategy scores, keyed by :class:`RetrievalStrategy` value."""
    ranks: dict[str, int] = field(default_factory=dict)
    """1-based rank within each strategy's own result list, used by RRF."""
    fused_score: float = 0.0
    rerank_score: float | None = None
    rerank_features: dict[str, float] = field(default_factory=dict)

    graph_evidence: tuple[GraphEvidence, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def final_score(self) -> float:
        """The score to order by: rerank score when present, fused score otherwise."""
        return self.fused_score if self.rerank_score is None else self.rerank_score

    @property
    def is_citable(self) -> bool:
        """Whether this candidate may be cited as a source of truth.

        Generated summary chunks are retrieval scaffolding synthesised from structured
        data; citing one would attribute a claim to a passage the clinician never wrote
        (docs/architecture/02-rag-hybrid-retrieval.md §1.2).
        """
        section = self.section_name or self.metadata.get("section_type") or ""
        return not str(section).startswith("generated_")

    def with_rank(self, strategy: RetrievalStrategy, rank: int, score: float) -> RetrievalCandidate:
        """Return a copy recording this strategy's rank and score."""
        return replace(
            self,
            ranks={**self.ranks, str(strategy): rank},
            scores={**self.scores, str(strategy): score},
        )

    def merged_with(self, other: RetrievalCandidate) -> RetrievalCandidate:
        """Combine two views of the same item found by different strategies.

        Field-level preference is "take whatever is populated" rather than "prefer self":
        the vector store and the graph return different metadata subsets for the same
        chunk, and preferring one side wholesale loses the other's contribution.
        """
        if self.id != other.id:
            raise ValueError(f"Cannot merge candidates with different ids: {self.id} != {other.id}")
        if self.tenant_id != other.tenant_id:
            # Fusion merges by id across strategies. Two stores returning the same id for
            # different tenants would blend them into one candidate carrying `self`'s
            # tenant, laundering the other tenant's text past every downstream check.
            raise ValueError(
                f"Cannot merge candidate '{self.id}' across tenants: "
                f"{self.tenant_id} != {other.tenant_id}"
            )
        return replace(
            self,
            text=self.text or other.text,
            document_id=self.document_id or other.document_id,
            chunk_index=self.chunk_index if self.chunk_index is not None else other.chunk_index,
            section_name=self.section_name or other.section_name,
            section_heading=self.section_heading or other.section_heading,
            page_start=self.page_start if self.page_start is not None else other.page_start,
            page_end=self.page_end if self.page_end is not None else other.page_end,
            effective_date=self.effective_date or other.effective_date,
            document_type=self.document_type or other.document_type,
            source_system=self.source_system or other.source_system,
            scores={**other.scores, **self.scores},
            ranks={**other.ranks, **self.ranks},
            graph_evidence=self.graph_evidence + other.graph_evidence,
            metadata={**other.metadata, **self.metadata},
        )


@dataclass(frozen=True, slots=True)
class RetrievalTrace:
    """Why a retrieval returned what it did.

    Populated on every request, not only on failures. The tracing requirement in
    docs/architecture/02-rag-hybrid-retrieval.md §4 exists because "why did this chunk
    surface" is unanswerable after the fact otherwise, and a retrieval system whose
    behaviour cannot be explained cannot be tuned or defended in an audit.
    """

    intent: QueryIntent
    intent_confidence: float
    strategies_dispatched: tuple[RetrievalStrategy, ...]
    weights: dict[str, float]
    candidates_per_strategy: dict[str, int]
    fused_count: int
    reranked_count: int
    returned_count: int
    filtered_by_acl: int = 0
    filtered_by_threshold: int = 0
    stage_durations_ms: dict[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "intent": str(self.intent),
            "intent_confidence": round(self.intent_confidence, 4),
            "strategies_dispatched": [str(s) for s in self.strategies_dispatched],
            "weights": {k: round(v, 4) for k, v in self.weights.items()},
            "candidates_per_strategy": dict(self.candidates_per_strategy),
            "fused_count": self.fused_count,
            "reranked_count": self.reranked_count,
            "returned_count": self.returned_count,
            "filtered_by_acl": self.filtered_by_acl,
            "filtered_by_threshold": self.filtered_by_threshold,
            "stage_durations_ms": {k: round(v, 3) for k, v in self.stage_durations_ms.items()},
            "notes": list(self.notes),
        }
