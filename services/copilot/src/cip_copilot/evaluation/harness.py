"""Copilot evaluation.

Phase 2 measured retrieval. This measures what happens after it: does the planner pick the
right capabilities, do claims survive verification, does the graph get used when it should,
what does a turn cost, and how often does the system correctly decline.

The last one is the metric most easily missed. A copilot that answers everything scores well
on "answered" and badly on patient safety, so **abstention correctness** is scored explicitly:
a case labelled unanswerable that produces an answer is a failure, exactly as a case labelled
answerable that produces a refusal is.
"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from cip_copilot.domain import ResponseMode
from cip_core.logging import get_logger

__all__ = [
    "CopilotEvalCase",
    "CopilotEvalReport",
    "CopilotEvaluator",
    "CostModel",
]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CostModel:
    """Per-1k-token rates for cost estimation.

    Injected rather than hardcoded: rates are provider- and contract-specific, and a number
    baked into the harness would silently go stale and misreport spend.
    """

    prompt_per_1k: float = 0.0
    completion_per_1k: float = 0.0
    label: str = "local"


@dataclass(frozen=True, slots=True)
class CopilotEvalCase:
    """One labelled question."""

    case_id: str
    question: str
    expected_mode: ResponseMode = ResponseMode.ANSWER
    expected_capabilities: frozenset[str] = frozenset()
    """Capabilities the planner should choose. Scored as recall — a plan that also runs
    something extra is wasteful, not wrong, while one that misses a capability answers a
    different question than the one asked."""
    must_cite_kinds: frozenset[str] = frozenset()
    forbidden_substrings: tuple[str, ...] = ()
    """Text that must not appear. The direct test for a specific hallucination."""
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    """What one case produced."""

    case_id: str
    mode: str
    latency_ms: float
    metrics: dict[str, float] = field(default_factory=dict)
    failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CopilotEvalReport:
    """Aggregate results."""

    case_count: int
    metrics: dict[str, float]
    latency_ms: dict[str, float]
    tokens: dict[str, float]
    cost_usd: float
    outcomes: tuple[CaseOutcome, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "case_count": self.case_count,
            "metrics": {k: round(v, 4) for k, v in self.metrics.items()},
            "latency_ms": {k: round(v, 2) for k, v in self.latency_ms.items()},
            "tokens": {k: round(v, 1) for k, v in self.tokens.items()},
            "cost_usd": self.cost_usd,
        }

    def summary(self) -> str:
        lines = [f"Copilot evaluation over {self.case_count} case(s)"]
        for name, value in sorted(self.metrics.items()):
            lines.append(f"  {name:<26} {value:.4f}")
        lines.append(
            f"  latency p50/p95/max      {self.latency_ms['p50']:.1f} / "
            f"{self.latency_ms['p95']:.1f} / {self.latency_ms['max']:.1f} ms"
        )
        lines.append(
            f"  tokens per turn (mean)   {self.tokens['mean_total']:.0f} "
            f"(prompt {self.tokens['mean_prompt']:.0f})"
        )
        lines.append(f"  estimated cost           ${self.cost_usd:.6f}")
        return "\n".join(lines)

    def failures(self) -> tuple[str, ...]:
        return tuple(
            f"{outcome.case_id}: {failure}"
            for outcome in self.outcomes
            for failure in outcome.failures
        )


AskFn = Callable[[CopilotEvalCase], Awaitable[Any]]


class CopilotEvaluator:
    """Scores a copilot against a labelled case set."""

    def __init__(self, *, cost_model: CostModel | None = None) -> None:
        self._cost = cost_model or CostModel()

    async def run(self, cases: Sequence[CopilotEvalCase], ask: AskFn) -> CopilotEvalReport:
        """Run every case and aggregate."""
        if not cases:
            raise ValueError("Cannot evaluate an empty case set")

        outcomes: list[CaseOutcome] = []
        prompt_tokens: list[int] = []
        completion_tokens: list[int] = []

        for case in cases:
            started = time.perf_counter()
            result = await ask(case)
            latency = (time.perf_counter() - started) * 1000

            answer = result.answer
            failures: list[str] = []
            metrics: dict[str, float] = {}

            mode_ok = answer.mode is case.expected_mode
            metrics["mode_correct"] = float(mode_ok)
            if not mode_ok:
                failures.append(f"expected mode {case.expected_mode}, got {answer.mode}")

            # Abstention is scored on the cases where it is the labelled outcome, so a system
            # that refuses everything cannot score well by accident.
            if case.expected_mode is not ResponseMode.ANSWER:
                metrics["abstention_correct"] = float(mode_ok)
            else:
                metrics["answer_rate"] = float(answer.mode is ResponseMode.ANSWER)

            planned = _planned_capabilities(answer)
            if case.expected_capabilities:
                hit = case.expected_capabilities & planned
                metrics["planner_recall"] = len(hit) / len(case.expected_capabilities)
                missing = case.expected_capabilities - planned
                if missing:
                    failures.append(f"planner missed {', '.join(sorted(missing))}")

            if answer.claims:
                verified = sum(1 for c in answer.claims if c.verified)
                metrics["claim_verification_rate"] = verified / len(answer.claims)
            metrics["hallucination_rate"] = _hallucination_rate(answer)
            # Citation and graph metrics are undefined when nothing was asserted. Scoring a
            # correctly-blocked turn as 0.0 would make a working abstention look like a
            # citation defect in the aggregate.
            if answer.claims:
                metrics["citation_rate"] = _citation_rate(answer)
                metrics["graph_utilisation"] = _graph_utilisation(answer)

            if case.must_cite_kinds:
                kinds = {str(item.kind) for item in answer.cited_evidence()}
                satisfied = case.must_cite_kinds <= kinds
                metrics["required_kinds_present"] = float(satisfied)
                if not satisfied:
                    failures.append(
                        f"missing evidence kinds {', '.join(sorted(case.must_cite_kinds - kinds))}"
                    )

            lowered = answer.text.lower()
            for forbidden in case.forbidden_substrings:
                if forbidden.lower() in lowered:
                    failures.append(f"answer contains forbidden text '{forbidden}'")
                    metrics["forbidden_text"] = 1.0

            prompt_tokens.append(answer.usage.prompt_tokens)
            completion_tokens.append(answer.usage.completion_tokens)

            outcomes.append(
                CaseOutcome(
                    case_id=case.case_id,
                    mode=str(answer.mode),
                    latency_ms=latency,
                    metrics=metrics,
                    failures=tuple(failures),
                )
            )

        return self._aggregate(outcomes, prompt_tokens, completion_tokens)

    def _aggregate(
        self,
        outcomes: list[CaseOutcome],
        prompt_tokens: list[int],
        completion_tokens: list[int],
    ) -> CopilotEvalReport:
        names = sorted({name for outcome in outcomes for name in outcome.metrics})
        aggregated = {
            name: statistics.fmean([o.metrics[name] for o in outcomes if name in o.metrics])
            for name in names
        }

        latencies = sorted(o.latency_ms for o in outcomes)
        latency = {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": latencies[-1],
            "mean": statistics.fmean(latencies),
        }

        total_prompt = sum(prompt_tokens)
        total_completion = sum(completion_tokens)
        tokens = {
            "mean_prompt": statistics.fmean(prompt_tokens) if prompt_tokens else 0.0,
            "mean_completion": statistics.fmean(completion_tokens) if completion_tokens else 0.0,
            "mean_total": (
                statistics.fmean(
                    [p + c for p, c in zip(prompt_tokens, completion_tokens, strict=True)]
                )
                if prompt_tokens
                else 0.0
            ),
            "total": float(total_prompt + total_completion),
        }
        cost = round(
            total_prompt / 1000 * self._cost.prompt_per_1k
            + total_completion / 1000 * self._cost.completion_per_1k,
            6,
        )

        report = CopilotEvalReport(
            case_count=len(outcomes),
            metrics=aggregated,
            latency_ms=latency,
            tokens=tokens,
            cost_usd=cost,
            outcomes=tuple(outcomes),
        )
        _log.info("copilot_evaluation.completed", **report.to_json())
        return report


def _planned_capabilities(answer: Any) -> frozenset[str]:
    """Capabilities the executor actually ran, from the trace."""
    for record in answer.trace:
        if record.stage == "execute":
            return frozenset(record.details.get("capabilities", ()))
    return frozenset()


def _hallucination_rate(answer: Any) -> float:
    """Fraction of claims rejected for asserting unsupported content."""
    for record in answer.trace:
        if record.stage == "reflect":
            return float(record.details.get("hallucination_rate", 0.0))
    return 0.0


def _citation_rate(answer: Any) -> float:
    """Fraction of surviving claims that resolve to evidence in the answer.

    Should be 1.0 by construction — a claim cannot be built without evidence ids, and
    verification drops unresolvable citations. Measured anyway: an invariant nobody checks is
    an invariant that eventually stops holding.
    """
    if not answer.claims:
        return 0.0
    available = {item.id for item in answer.evidence}
    resolvable = sum(1 for claim in answer.claims if set(claim.evidence_ids) <= available)
    return resolvable / len(answer.claims)


def _graph_utilisation(answer: Any) -> float:
    """Fraction of cited evidence that came from the knowledge graph."""
    cited = answer.cited_evidence()
    if not cited:
        return 0.0
    graph = sum(1 for item in cited if str(item.kind) == "graph_relationship")
    return graph / len(cited)


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, math.ceil(fraction * len(sorted_values)) - 1))
    return sorted_values[index]
