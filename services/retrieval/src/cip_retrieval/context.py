"""Context assembly.

Turns ranked candidates into the bounded, cited block of text a model is given. Four
decisions here materially affect answer quality.

**The budget is a hard ceiling, enforced before the model sees anything.** Overflowing it
means the provider truncates — from the end, with no knowledge of what it is cutting — and
the citation the answer depends on can vanish while the answer still sounds confident.

**Deduplication is by content, not id.** Chunk overlap means adjacent chunks legitimately
share text, and re-ingested documents produce different ids for identical content. Packing
both wastes budget on a passage the model has already read.

**Citations are numbered in presentation order.** ``[1]`` must be the first block the model
sees, or the model's own reference numbering will disagree with the citation list — which
looks like a hallucinated citation during review even when the retrieval was correct.

**Graph evidence is rendered as attributed assertions, not prose.** A graph edge is an
inference with a confidence and a source; presenting it as a quotation would let the model
treat it as something a clinician wrote.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cip_core.logging import get_logger
from cip_ingestion.processing.tokenization import HeuristicTokenEstimator, TokenEstimator
from cip_retrieval.domain import (
    GraphEvidence,
    RetrievalCandidate,
    RetrievalTrace,
    SourceKind,
    content_digest_text,
)

__all__ = ["AssembledContext", "ContextBlock", "ContextBudget", "ContextBuilder"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Token allocation for the assembled context.

    ``reserved_for_answer`` is subtracted up front. Without it the context can fill the
    entire window and leave the model no room to answer — which manifests as a truncated
    or empty response rather than an obvious error.
    """

    max_context_tokens: int = 6000
    reserved_for_answer: int = 1000
    max_graph_evidence: int = 12
    max_blocks: int = 20

    def __post_init__(self) -> None:
        if self.max_context_tokens < 1:
            raise ValueError("max_context_tokens must be >= 1")
        if self.reserved_for_answer < 0:
            raise ValueError("reserved_for_answer must be >= 0")
        if self.reserved_for_answer >= self.max_context_tokens:
            raise ValueError("reserved_for_answer must be less than max_context_tokens")

    @property
    def available_tokens(self) -> int:
        return self.max_context_tokens - self.reserved_for_answer


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """One citable unit of assembled context."""

    citation_index: int
    text: str
    token_count: int
    source_kind: SourceKind
    candidate_id: str
    document_id: str | None = None
    section_heading: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    document_type: str | None = None
    effective_date: str | None = None
    citable: bool = True
    """False for generated summary chunks, which are retrieval scaffolding rather than
    something a clinician wrote (docs/architecture/02-rag-hybrid-retrieval.md §1.2)."""

    def render(self) -> str:
        """Render with a citation marker and provenance header."""
        parts = [f"[{self.citation_index}]"]
        if self.section_heading:
            parts.append(self.section_heading)
        if self.document_type:
            parts.append(f"({self.document_type})")
        if self.page_start is not None:
            pages = (
                f"p.{self.page_start}"
                if self.page_end in (None, self.page_start)
                else f"pp.{self.page_start}-{self.page_end}"
            )
            parts.append(pages)
        if self.effective_date:
            parts.append(self.effective_date)
        if not self.citable:
            parts.append("[generated summary — not citable]")
        return f"{' '.join(parts)}\n{self.text}"


@dataclass(frozen=True, slots=True)
class AssembledContext:
    """The finished context, ready for prompt rendering."""

    blocks: tuple[ContextBlock, ...]
    graph_evidence: tuple[GraphEvidence, ...]
    total_tokens: int
    budget: ContextBudget
    dropped_duplicates: int = 0
    dropped_over_budget: int = 0
    trace: RetrievalTrace | None = None

    @property
    def is_empty(self) -> bool:
        """No evidence was assembled.

        The pipeline's no-evidence gate keys off this: an empty context must never reach a
        model, because a model with no context answers from parametric memory
        (docs/architecture/02-rag-hybrid-retrieval.md §2.4).
        """
        return not self.blocks and not self.graph_evidence

    def render_evidence(self) -> str:
        """The document-passage section of the prompt."""
        return "\n\n".join(block.render() for block in self.blocks)

    def render_graph_evidence(self) -> str:
        """Graph relationships as attributed assertions with confidence."""
        if not self.graph_evidence:
            return ""
        lines = []
        for evidence in self.graph_evidence:
            attribution = f"confidence {evidence.confidence:.2f}"
            if evidence.evidence_level:
                attribution += f", {evidence.evidence_level}"
            if evidence.hops > 1:
                attribution += f", {evidence.hops} hops"
            lines.append(f"- {evidence.as_sentence()} ({attribution})")
        return "\n".join(lines)

    def citation_map(self) -> dict[int, str]:
        """Citation index to candidate id, for verifying the model's references."""
        return {block.citation_index: block.candidate_id for block in self.blocks}

    def to_json(self) -> dict[str, Any]:
        return {
            "block_count": len(self.blocks),
            "graph_evidence_count": len(self.graph_evidence),
            "total_tokens": self.total_tokens,
            "available_tokens": self.budget.available_tokens,
            "utilisation": round(self.total_tokens / max(self.budget.available_tokens, 1), 4),
            "dropped_duplicates": self.dropped_duplicates,
            "dropped_over_budget": self.dropped_over_budget,
            "trace": self.trace.to_json() if self.trace else None,
        }


class ContextBuilder:
    """Packs ranked candidates into a token-bounded, cited context."""

    def __init__(
        self,
        budget: ContextBudget | None = None,
        *,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        self._budget = budget
        self._tokens = token_estimator or HeuristicTokenEstimator()

    def build(
        self,
        candidates: list[RetrievalCandidate],
        *,
        budget: ContextBudget | None = None,
        trace: RetrievalTrace | None = None,
    ) -> AssembledContext:
        """Assemble context from candidates in their existing (ranked) order.

        Order is preserved rather than re-sorted: ranking already happened, and reordering
        here would silently override the reranker.
        """
        budget = budget or self._budget or ContextBudget()

        blocks: list[ContextBlock] = []
        seen_content: set[str] = set()
        graph_evidence: list[GraphEvidence] = []
        seen_evidence: set[tuple[str, str, str]] = set()

        used_tokens = 0
        dropped_duplicates = 0
        dropped_over_budget = 0

        for candidate in candidates:
            if candidate.source_kind in (SourceKind.GRAPH_ENTITY, SourceKind.GRAPH_PATH):
                for evidence in candidate.graph_evidence:
                    key = (evidence.subject, evidence.predicate, evidence.object)
                    if key in seen_evidence:
                        continue
                    if len(graph_evidence) >= budget.max_graph_evidence:
                        break
                    # Graph evidence is charged to the same budget as passages. It used to
                    # be counted only *after* packing, which meant the reported total could
                    # exceed the ceiling this class exists to enforce: blocks filled the
                    # budget, then up to `max_graph_evidence` lines were appended for free
                    # and the provider truncated the tail — dropping exactly the citations
                    # the answer depends on.
                    cost = self._tokens.count(f"- {evidence.as_sentence()}\n")
                    if used_tokens + cost > budget.available_tokens:
                        dropped_over_budget += 1
                        break
                    seen_evidence.add(key)
                    used_tokens += cost
                    graph_evidence.append(evidence)
                continue

            text = candidate.text.strip()
            if not text:
                continue

            digest = content_digest_text(text)
            if digest in seen_content:
                dropped_duplicates += 1
                continue

            if len(blocks) >= budget.max_blocks:
                dropped_over_budget += 1
                continue

            # Cost the *rendered* block, not the raw text: the citation marker and
            # provenance header are real tokens the model receives, and ignoring them
            # overruns the budget by a few percent on every block.
            provisional = ContextBlock(
                citation_index=len(blocks) + 1,
                text=text,
                token_count=0,
                source_kind=candidate.source_kind,
                candidate_id=candidate.id,
                document_id=str(candidate.document_id) if candidate.document_id else None,
                section_heading=candidate.section_heading,
                page_start=candidate.page_start,
                page_end=candidate.page_end,
                document_type=candidate.document_type,
                effective_date=(
                    candidate.effective_date.isoformat() if candidate.effective_date else None
                ),
                citable=candidate.is_citable,
            )
            cost = self._tokens.count(provisional.render())

            if used_tokens + cost > budget.available_tokens:
                # Keep scanning rather than stopping: a later candidate may be small enough
                # to fit, and stopping would waste the remaining budget entirely.
                dropped_over_budget += 1
                continue

            seen_content.add(digest)
            used_tokens += cost
            blocks.append(
                ContextBlock(
                    citation_index=provisional.citation_index,
                    text=provisional.text,
                    token_count=cost,
                    source_kind=provisional.source_kind,
                    candidate_id=provisional.candidate_id,
                    document_id=provisional.document_id,
                    section_heading=provisional.section_heading,
                    page_start=provisional.page_start,
                    page_end=provisional.page_end,
                    document_type=provisional.document_type,
                    effective_date=provisional.effective_date,
                    citable=provisional.citable,
                )
            )

        _log.debug(
            "context.assembled",
            blocks=len(blocks),
            graph_evidence=len(graph_evidence),
            tokens=used_tokens,
            budget=budget.available_tokens,
            dropped_duplicates=dropped_duplicates,
            dropped_over_budget=dropped_over_budget,
        )

        return AssembledContext(
            blocks=tuple(blocks),
            graph_evidence=tuple(graph_evidence),
            total_tokens=used_tokens,
            budget=budget,
            dropped_duplicates=dropped_duplicates,
            dropped_over_budget=dropped_over_budget,
            trace=trace,
        )
