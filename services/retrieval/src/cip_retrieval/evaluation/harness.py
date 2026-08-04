"""Evaluation harness.

Runs a labelled eval set through a retrieval callable and reports aggregate metrics. The
point is comparability: any change — a new embedding model, different fusion weights, a
cross-encoder reranker — is scored on the same set, so "better" is a measurement rather
than an impression.

Latency is recorded per case and reported as percentiles rather than a mean. A mean hides
the tail, and the tail is what a clinician experiences as "the system is slow".

Graph coverage is reported alongside relevance metrics because it explains *why* a
graph-dominant query underperformed: a low score with zero graph coverage is a knowledge-
graph gap, not a retrieval-algorithm failure, and the two demand entirely different fixes.
"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from cip_core.logging import get_logger
from cip_retrieval.domain import QueryIntent, RetrievalCandidate
from cip_retrieval.evaluation.metrics import (
    average_precision,
    context_precision,
    context_recall,
    hit_rate,
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)

__all__ = ["EvalCase", "EvalReport", "EvalResult", "RetrievalEvaluator"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One labelled question.

    ``relevant_ids`` is the binary judgement; ``graded_relevance`` optionally refines it
    for NDCG. Where both are given, the graded map is authoritative for NDCG and the binary
    set for everything else — keeping them separate avoids forcing every case to be graded.
    """

    case_id: str
    question: str
    relevant_ids: set[str]
    graded_relevance: dict[str, float] = field(default_factory=dict)
    expected_intent: QueryIntent | None = None
    requires_graph: bool = False
    """Marks a case that *should* be answered via graph relationships. Lets graph coverage
    be measured against cases where it matters rather than diluted across all of them."""
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.relevant_ids:
            raise ValueError(f"Eval case '{self.case_id}' has no relevant ids")


@dataclass(frozen=True, slots=True)
class EvalResult:
    """Per-case outcome."""

    case_id: str
    retrieved_ids: tuple[str, ...]
    context_ids: tuple[str, ...]
    latency_ms: float
    routed_intent: QueryIntent | None = None
    graph_candidates: int = 0
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvalReport:
    """Aggregate report across an eval set."""

    case_count: int
    metrics: dict[str, float]
    latency_ms: dict[str, float]
    intent_accuracy: float | None
    graph_coverage: float
    results: tuple[EvalResult, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "case_count": self.case_count,
            "metrics": {k: round(v, 4) for k, v in self.metrics.items()},
            "latency_ms": {k: round(v, 2) for k, v in self.latency_ms.items()},
            "intent_accuracy": (
                round(self.intent_accuracy, 4) if self.intent_accuracy is not None else None
            ),
            "graph_coverage": round(self.graph_coverage, 4),
        }

    def summary(self) -> str:
        """Human-readable one-block summary for CI logs."""
        lines = [f"Evaluation over {self.case_count} case(s)"]
        for name, value in sorted(self.metrics.items()):
            lines.append(f"  {name:<22} {value:.4f}")
        lines.append(f"  {'graph_coverage':<22} {self.graph_coverage:.4f}")
        if self.intent_accuracy is not None:
            lines.append(f"  {'intent_accuracy':<22} {self.intent_accuracy:.4f}")
        lines.append(
            f"  latency p50/p95/max    {self.latency_ms['p50']:.1f} / "
            f"{self.latency_ms['p95']:.1f} / {self.latency_ms['max']:.1f} ms"
        )
        return "\n".join(lines)


#: What the pipeline must hand back for a case to be scored.
RetrieveFn = Callable[[EvalCase], Awaitable[tuple[list[RetrievalCandidate], Any]]]


class RetrievalEvaluator:
    """Scores a retrieval function against a labelled eval set."""

    def __init__(self, *, k_values: Sequence[int] = (1, 3, 5, 10)) -> None:
        self._k_values = tuple(sorted(k_values))

    async def run(self, cases: Sequence[EvalCase], retrieve: RetrieveFn) -> EvalReport:
        """Execute every case and aggregate.

        ``retrieve`` returns ``(candidates, context)`` where ``context`` may be an
        :class:`~cip_retrieval.context.AssembledContext` or ``None``. Accepting ``None``
        lets raw retrieval be evaluated without context assembly, which is how the effect
        of a budget change is isolated from the effect of a ranking change.
        """
        if not cases:
            raise ValueError("Cannot evaluate an empty case set")

        results: list[EvalResult] = []

        for case in cases:
            started = time.perf_counter()
            candidates, context = await retrieve(case)
            latency_ms = (time.perf_counter() - started) * 1000

            retrieved_ids = tuple(candidate.id for candidate in candidates)
            context_ids = tuple(block.candidate_id for block in getattr(context, "blocks", ()))
            graph_candidates = sum(1 for candidate in candidates if candidate.graph_evidence)

            per_case: dict[str, float] = {}
            for k in self._k_values:
                per_case[f"precision@{k}"] = precision_at_k(retrieved_ids, case.relevant_ids, k)
                per_case[f"recall@{k}"] = recall_at_k(retrieved_ids, case.relevant_ids, k)
                per_case[f"ndcg@{k}"] = ndcg_at_k(
                    retrieved_ids,
                    case.graded_relevance or dict.fromkeys(case.relevant_ids, 1.0),
                    k,
                )
            per_case["average_precision"] = average_precision(retrieved_ids, case.relevant_ids)
            if context_ids:
                per_case["context_precision"] = context_precision(context_ids, case.relevant_ids)
                per_case["context_recall"] = context_recall(context_ids, case.relevant_ids)

            trace = getattr(context, "trace", None)
            results.append(
                EvalResult(
                    case_id=case.case_id,
                    retrieved_ids=retrieved_ids,
                    context_ids=context_ids,
                    latency_ms=latency_ms,
                    routed_intent=getattr(trace, "intent", None),
                    graph_candidates=graph_candidates,
                    metrics=per_case,
                )
            )

        return self._aggregate(cases, results)

    def _aggregate(self, cases: Sequence[EvalCase], results: Sequence[EvalResult]) -> EvalReport:
        metric_names = sorted({name for result in results for name in result.metrics})
        aggregated = {
            name: statistics.fmean(
                [result.metrics[name] for result in results if name in result.metrics]
            )
            for name in metric_names
        }

        paired = [
            (result.retrieved_ids, case.relevant_ids)
            for case, result in zip(cases, results, strict=True)
        ]
        aggregated["mrr"] = mean_reciprocal_rank(paired)
        aggregated["hit_rate@10"] = hit_rate(paired, k=10)

        latencies = sorted(result.latency_ms for result in results)
        latency = {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": latencies[-1],
            "mean": statistics.fmean(latencies),
        }

        # Intent accuracy is scored only over cases that declared an expectation, so an
        # unlabelled majority cannot dilute the signal into meaninglessness.
        intent_cases = [
            (case, result)
            for case, result in zip(cases, results, strict=True)
            if case.expected_intent is not None
        ]
        intent_accuracy = (
            sum(1 for case, result in intent_cases if result.routed_intent == case.expected_intent)
            / len(intent_cases)
            if intent_cases
            else None
        )

        graph_cases = [
            (case, result)
            for case, result in zip(cases, results, strict=True)
            if case.requires_graph
        ]
        graph_coverage = (
            sum(1 for _, result in graph_cases if result.graph_candidates > 0) / len(graph_cases)
            if graph_cases
            else 0.0
        )

        report = EvalReport(
            case_count=len(cases),
            metrics=aggregated,
            latency_ms=latency,
            intent_accuracy=intent_accuracy,
            graph_coverage=graph_coverage,
            results=tuple(results),
        )
        _log.info("evaluation.completed", **report.to_json())
        return report


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile over a pre-sorted sequence."""
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, math.ceil(fraction * len(sorted_values)) - 1))
    return sorted_values[index]
