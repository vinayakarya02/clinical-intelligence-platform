"""Retrieval and generation metrics.

Pure functions over ranked ids and relevance judgements — no I/O, no pipeline coupling — so
any retrieval configuration can be scored the same way and compared.

Two conventions worth stating, because they are where metric implementations usually differ
silently:

**Ranks are 1-based**, matching how retrieval results are presented and how RRF consumes
them. An off-by-one here shifts every discount in NDCG.

**An empty result scores 0, not NaN.** A configuration that returns nothing has *failed*
the query; propagating NaN would let it silently vanish from an average and make a broken
retriever look untested rather than bad.

Graded relevance is supported where the metric admits it (NDCG), because clinical relevance
is genuinely graded: a chunk that directly answers a lab query and one that merely mentions
the analyte are not equally useful, and collapsing them to binary discards information the
eval set already has.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

__all__ = [
    "average_precision",
    "context_precision",
    "context_recall",
    "hit_rate",
    "mean_reciprocal_rank",
    "ndcg_at_k",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
]


def precision_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of the top ``k`` results that are relevant.

    Divides by ``k``, not by the number returned. Returning three results of which all
    three are relevant is not precision 1.0 at k=10 — it under-delivered, and dividing by
    the smaller count would hide that.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    if not retrieved:
        return 0.0
    top = retrieved[:k]
    return sum(1 for item in top if item in relevant) / k


def recall_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of all relevant items that appear in the top ``k``.

    Undefined with no relevant items, which is a malformed eval case rather than a
    retrieval outcome — returns 0.0 so one bad case cannot poison an aggregate with NaN.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    if not relevant:
        return 0.0
    return sum(1 for item in retrieved[:k] if item in relevant) / len(relevant)


def reciprocal_rank(retrieved: Sequence[str], relevant: set[str]) -> float:
    """``1 / rank`` of the first relevant result, or 0 if none is retrieved."""
    for index, item in enumerate(retrieved, start=1):
        if item in relevant:
            return 1.0 / index
    return 0.0


def mean_reciprocal_rank(
    results: Sequence[tuple[Sequence[str], set[str]]],
) -> float:
    """MRR across queries. Rewards putting *a* correct answer near the top."""
    if not results:
        return 0.0
    return sum(reciprocal_rank(retrieved, relevant) for retrieved, relevant in results) / len(
        results
    )


def hit_rate(results: Sequence[tuple[Sequence[str], set[str]]], k: int = 10) -> float:
    """Fraction of queries with at least one relevant result in the top ``k``.

    The bluntest useful metric: it answers "how often does retrieval work at all", which
    ranking-quality metrics can obscure when a few queries fail completely.
    """
    if not results:
        return 0.0
    hits = sum(
        1 for retrieved, relevant in results if any(item in relevant for item in retrieved[:k])
    )
    return hits / len(results)


def average_precision(retrieved: Sequence[str], relevant: set[str]) -> float:
    """Mean of precision values taken at each relevant hit."""
    if not relevant:
        return 0.0
    hits = 0
    total = 0.0
    for index, item in enumerate(retrieved, start=1):
        if item in relevant:
            hits += 1
            total += hits / index
    return total / len(relevant)


def _dcg(gains: Sequence[float]) -> float:
    """Discounted cumulative gain with the standard log2(rank + 1) discount."""
    return sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains, start=1))


def ndcg_at_k(
    retrieved: Sequence[str],
    relevance: Mapping[str, float],
    k: int,
) -> float:
    """Normalised DCG at ``k`` over graded relevance.

    ``relevance`` maps id to gain; anything absent scores 0. Normalising against the ideal
    ordering is what makes the metric comparable across queries whose relevant-set sizes
    differ — a query with one relevant document and a query with twenty are otherwise not
    on the same scale.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    if not relevance:
        return 0.0

    actual = _dcg([relevance.get(item, 0.0) for item in retrieved[:k]])
    ideal = _dcg(sorted(relevance.values(), reverse=True)[:k])
    return actual / ideal if ideal > 0 else 0.0


def context_precision(context_ids: Sequence[str], relevant: set[str]) -> float:
    """Fraction of assembled context that is relevant.

    Distinct from ``precision_at_k``: this scores what actually reached the model after
    budgeting and deduplication, not what retrieval returned. Low context precision means
    the model is paying attention tax on irrelevant passages — the mechanism behind
    context dilution.
    """
    if not context_ids:
        return 0.0
    return sum(1 for item in context_ids if item in relevant) / len(context_ids)


def context_recall(context_ids: Sequence[str], relevant: set[str]) -> float:
    """Fraction of relevant items that survived into the assembled context.

    The metric that catches a budget set too tight: retrieval can find everything and
    context assembly can still drop the answer.
    """
    if not relevant:
        return 0.0
    return sum(1 for item in relevant if item in set(context_ids)) / len(relevant)
