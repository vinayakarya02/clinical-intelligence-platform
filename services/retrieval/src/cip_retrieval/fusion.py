"""Reciprocal Rank Fusion.

RRF combines ranked lists by summing ``weight / (k + rank)`` across the strategies that
found each item. It consumes **ranks, not scores**, and that is the entire reason to use it
here: a cosine similarity of 0.83, a BM25 score of 14.2, and a graph path confidence of 0.36
are not comparable quantities. Any fusion that normalised and added them would be inventing
a common scale that does not exist, and the resulting ordering would shift whenever a
scoring function was retuned. Ranks are ordinal and comparable by construction.

The constant ``k`` (60 by convention) damps the difference between top ranks. Without it,
rank 1 would score twice rank 2 and a single strategy's top hit would dominate every fusion.
With it, an item ranked 3rd by two strategies beats an item ranked 1st by one — which is the
behaviour hybrid retrieval exists to produce.

Weights let a query type favour a strategy without silencing the others: a medication
interaction question weights the graph heavily but still admits a vector hit that the graph
missed.
"""

from __future__ import annotations

from dataclasses import dataclass

from cip_core.logging import get_logger
from cip_retrieval.domain import RetrievalCandidate, RetrievalStrategy

__all__ = ["FusionWeights", "reciprocal_rank_fusion"]

_log = get_logger(__name__)

#: Standard RRF damping constant (Cormack et al.). Larger flattens the contribution of top
#: ranks further; smaller lets a single strategy's leader dominate.
DEFAULT_RRF_K = 60


@dataclass(frozen=True, slots=True)
class FusionWeights:
    """Per-strategy fusion weights.

    A weight of 0 excludes a strategy from *scoring* but not from having run — the trace
    still records what it returned, which is what makes routing decisions auditable after
    the fact.
    """

    vector: float = 1.0
    keyword: float = 1.0
    graph: float = 1.0

    def __post_init__(self) -> None:
        for name, value in self.as_dict().items():
            if value < 0.0:
                raise ValueError(f"Fusion weight for {name} must be >= 0")
        if all(value == 0.0 for value in self.as_dict().values()):
            raise ValueError("At least one fusion weight must be non-zero")

    def as_dict(self) -> dict[str, float]:
        return {
            str(RetrievalStrategy.VECTOR): self.vector,
            str(RetrievalStrategy.KEYWORD): self.keyword,
            str(RetrievalStrategy.GRAPH): self.graph,
        }

    def for_strategy(self, strategy: RetrievalStrategy) -> float:
        return self.as_dict()[str(strategy)]


def reciprocal_rank_fusion(
    ranked_lists: dict[RetrievalStrategy, list[RetrievalCandidate]],
    *,
    weights: FusionWeights | None = None,
    k: int = DEFAULT_RRF_K,
    limit: int | None = None,
) -> list[RetrievalCandidate]:
    """Fuse per-strategy ranked lists into one ordering.

    Candidates found by more than one strategy are merged, accumulating every strategy's
    rank and score, so the trace shows *why* an item ranked where it did rather than only
    that it did.

    Args:
        ranked_lists: Each strategy's candidates, best first.
        weights: Per-strategy weights. Defaults to equal weighting.
        k: RRF damping constant.
        limit: Truncate the fused result. ``None`` returns everything.

    Returns:
        Candidates ordered by descending fused score.
    """
    if k < 1:
        raise ValueError("RRF k must be >= 1")

    weights = weights or FusionWeights()
    merged: dict[str, RetrievalCandidate] = {}
    scores: dict[str, float] = {}

    for strategy, candidates in ranked_lists.items():
        weight = weights.for_strategy(strategy)
        for candidate in candidates:
            existing = merged.get(candidate.id)
            merged[candidate.id] = (
                candidate if existing is None else existing.merged_with(candidate)
            )

            # Trust the rank the retriever recorded rather than list position: a retriever
            # may legitimately return a list whose order differs from its own ranking (a
            # stable-sort re-ordering, a post-filter), and position would then be wrong.
            rank = candidate.ranks.get(str(strategy))
            if rank is None:
                continue
            scores[candidate.id] = scores.get(candidate.id, 0.0) + weight / (k + rank)

    fused = [
        _with_fused_score(candidate, scores.get(candidate_id, 0.0))
        for candidate_id, candidate in merged.items()
    ]
    # Tie-break on id so equal-scoring results are stable across runs; an unstable order
    # makes evaluation runs irreproducible and A/B comparisons meaningless.
    fused.sort(key=lambda candidate: (-candidate.fused_score, candidate.id))

    _log.debug(
        "fusion.completed",
        strategies=len(ranked_lists),
        unique_candidates=len(fused),
        weights=weights.as_dict(),
    )
    return fused[:limit] if limit is not None else fused


def _with_fused_score(candidate: RetrievalCandidate, score: float) -> RetrievalCandidate:
    from dataclasses import replace

    return replace(candidate, fused_score=score)
