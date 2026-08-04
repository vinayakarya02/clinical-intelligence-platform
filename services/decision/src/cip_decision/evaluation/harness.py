"""Clinical decision evaluation.

Phase 2 measured retrieval and Phase 3 measured reasoning. This measures decisions, where the
quantities are different: a decision layer is not judged by how much it finds but by whether
what it finds is right, whether what it *misses* is safe, and whether the volume it produces is
something a clinician can actually read.

Four metrics carry the framework.

**Rule recall against labelled cases** — did the rules that should have fired, fire. The
straightforward one, and the least interesting, because a rule engine over a fixed corpus is
nearly always right about this.

**False-positive rate against forbidden labels.** A case may assert that a rule *must not*
fire. This is where the Phase 5 Blockers lived: a stroke score computed for a patient without
the arrhythmia and an allergy generalised across a whole drug class both look like healthy
recall and are clinically wrong. Every fixed Blocker has a forbidden label here.

**Alert burden.** Mean and maximum alerts shown per case, and what fraction suppression
removed. Published override rates for clinical decision support run 49–96%
(docs/design/adr-0021-alert-fatigue.md), so a system that raises its recall by showing more is
usually making itself worse. Burden is reported next to recall for exactly that reason.

**Rule coverage.** Which active rules no case exercises. An unexercised rule is knowledge
nobody has ever seen execute — it may be dead, mis-authored, or silently unevaluable, and no
accuracy metric will reveal it because it never contributes to one.

What this framework **cannot** measure is the number that matters most: the real-world override
rate. That requires clinicians using the system on their own patients, and no amount of
labelled offline data substitutes for it. The burden numbers here are an upper bound on what
would reach a screen, not evidence that the screen is tolerable.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from typing import Any

from cip_core.logging import get_logger
from cip_decision.domain import PatientContext, Severity
from cip_decision.engine import DecisionEngine
from cip_decision.rules.engine import RuleEngine
from cip_decision.suppression import SuppressionPolicy

__all__ = [
    "CaseOutcome",
    "DecisionEvalCase",
    "DecisionEvalReport",
    "DecisionEvaluator",
]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DecisionEvalCase:
    """One labelled patient.

    Labels are expressed as rule ids rather than recommendation text, because recommendation
    wording is knowledge-base content that a clinical reviewer is expected to edit. A suite
    keyed on wording would break on a copy-edit and pass on a logic change, which is precisely
    backwards.
    """

    case_id: str
    context: PatientContext

    expected_rule_ids: frozenset[str] = frozenset()
    """Rules that must fire. Scored as recall."""

    forbidden_rule_ids: frozenset[str] = frozenset()
    """Rules that must **not** fire. Each one is a specific false positive somebody has
    already been wrong about; a firing here is a safety failure, not a precision miss."""

    forbidden_risk_models: frozenset[str] = frozenset()
    """Risk models that must report *inapplicable* rather than a score. A stroke score on a
    patient without the arrhythmia it was derived in is a meaningless number that reads as
    authoritative (Blocker B1)."""

    expect_recommendations: bool | None = None
    """``True`` requires at least one shown recommendation, ``False`` requires none, ``None``
    does not check. ``False`` is the label that catches an engine that alerts on everything."""

    expected_missing: frozenset[str] = frozenset()
    """Substrings that must appear in the missing-information report. This is how a case
    asserts that the engine said "I could not evaluate this" rather than staying silent."""

    minimum_severity: Severity | None = None
    """The most severe recommendation must reach at least this. A case where a
    contraindication degrades to an informational note fails here."""

    policy: SuppressionPolicy | None = None
    """The role's suppression policy, so burden is measured as the role would experience it."""

    notes: str = ""


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    """What one case produced."""

    case_id: str
    fired_rule_ids: frozenset[str] = frozenset()
    shown: int = 0
    suppressed: int = 0
    latency_ms: float = 0.0
    failures: tuple[str, ...] = ()
    safety_failures: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.failures and not self.safety_failures


@dataclass(frozen=True, slots=True)
class DecisionEvalReport:
    """Aggregate results across a suite.

    Accuracy and burden are reported together and never combined into one score. A single
    number would let a change that raises recall by alerting more often look like an
    improvement, which is the failure mode the alert-fatigue literature describes.
    """

    case_count: int = 0
    passed: int = 0

    rule_recall: float = 0.0
    rule_precision: float = 0.0
    false_positive_count: int = 0

    mean_alerts_per_case: float = 0.0
    max_alerts_in_a_case: int = 0
    suppression_rate: float = 0.0
    silent_case_rate: float = 0.0

    rule_coverage: float = 0.0
    uncovered_rules: tuple[str, ...] = ()

    explanation_completeness: float = 0.0
    contraindications_suppressed: int = 0
    deterministic: bool = True

    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0

    outcomes: tuple[CaseOutcome, ...] = ()

    @property
    def safety_failures(self) -> tuple[str, ...]:
        return tuple(f for o in self.outcomes for f in o.safety_failures)

    @property
    def is_clean(self) -> bool:
        """Whether the suite may be considered passing.

        Deliberately strict about the safety-relevant invariants: a suppressed
        contraindication, a non-deterministic decision, or any forbidden firing fails the run
        outright regardless of how good the accuracy numbers are.
        """
        return (
            self.passed == self.case_count
            and not self.safety_failures
            and self.contraindications_suppressed == 0
            and self.deterministic
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "cases": self.case_count,
            "passed": self.passed,
            "rule_recall": round(self.rule_recall, 4),
            "rule_precision": round(self.rule_precision, 4),
            "false_positives": self.false_positive_count,
            "mean_alerts_per_case": round(self.mean_alerts_per_case, 3),
            "max_alerts_in_a_case": self.max_alerts_in_a_case,
            "suppression_rate": round(self.suppression_rate, 4),
            "silent_case_rate": round(self.silent_case_rate, 4),
            "rule_coverage": round(self.rule_coverage, 4),
            "uncovered_rules": list(self.uncovered_rules),
            "explanation_completeness": round(self.explanation_completeness, 4),
            "contraindications_suppressed": self.contraindications_suppressed,
            "deterministic": self.deterministic,
            "p50_latency_ms": round(self.p50_latency_ms, 3),
            "p95_latency_ms": round(self.p95_latency_ms, 3),
            "safety_failures": list(self.safety_failures),
        }

    def render(self) -> str:
        lines = [
            f"  cases                        {self.passed}/{self.case_count} passed",
            f"  rule recall                  {self.rule_recall:.1%}",
            f"  rule precision               {self.rule_precision:.1%}",
            f"  false positives              {self.false_positive_count}",
            f"  alerts per case              {self.mean_alerts_per_case:.2f} mean, "
            f"{self.max_alerts_in_a_case} max",
            f"  suppressed by policy         {self.suppression_rate:.1%}",
            f"  cases with no alert          {self.silent_case_rate:.1%}",
            f"  rule coverage                {self.rule_coverage:.1%}",
            f"  explanation completeness     {self.explanation_completeness:.1%}",
            f"  contraindications suppressed {self.contraindications_suppressed}",
            f"  deterministic                {'yes' if self.deterministic else 'NO'}",
            f"  latency                      p50 {self.p50_latency_ms:.2f} ms, "
            f"p95 {self.p95_latency_ms:.2f} ms",
        ]
        if self.uncovered_rules:
            lines.append("  rules no case exercises      " + ", ".join(self.uncovered_rules))
        for outcome in self.outcomes:
            for failure in (*outcome.safety_failures, *outcome.failures):
                lines.append(f"    ! {outcome.case_id}: {failure}")
        return "\n".join(lines)


class DecisionEvaluator:
    """Runs a labelled suite against a decision engine."""

    def __init__(self, engine: DecisionEngine, *, rules: RuleEngine) -> None:
        self._engine = engine
        self._rules = rules
        """Held only to enumerate active rules for the coverage metric. The evaluator never
        evaluates a rule itself — a harness that reimplemented evaluation would be testing its
        own copy of the logic."""

    def run(self, cases: list[DecisionEvalCase]) -> DecisionEvalReport:
        if not cases:
            return DecisionEvalReport()

        outcomes: list[CaseOutcome] = []
        latencies: list[float] = []
        shown_counts: list[int] = []
        suppressed_total = 0
        expected_total = 0
        matched_total = 0
        fired_total = 0
        false_positives = 0
        explained = 0
        explainable = 0
        contraindications_suppressed = 0
        deterministic = True
        covered: set[str] = set()

        for case in cases:
            started = time.perf_counter()
            result = self._engine.decide(case.context, policy=case.policy)
            latency = (time.perf_counter() - started) * 1000
            latencies.append(latency)

            fired = frozenset(o.rule.rule_id for o in result.rule_trace.fired)
            covered |= fired

            failures: list[str] = []
            safety: list[str] = []

            matched = fired & case.expected_rule_ids
            for missing in sorted(case.expected_rule_ids - fired):
                failures.append(f"expected rule '{missing}' did not fire")
            expected_total += len(case.expected_rule_ids)
            matched_total += len(matched)
            fired_total += len(fired)

            for forbidden in sorted(fired & case.forbidden_rule_ids):
                safety.append(f"rule '{forbidden}' fired and must not have")
                false_positives += 1

            for model in sorted(case.forbidden_risk_models):
                scored = [
                    r for r in result.risk_results if r.model.model_id == model and r.applicable
                ]
                if scored:
                    safety.append(
                        f"risk model '{model}' produced a score for a patient it does not apply to"
                    )
                    false_positives += 1

            if case.expect_recommendations is True and not result.recommendations:
                failures.append("expected at least one recommendation, got none")
            if case.expect_recommendations is False and result.recommendations:
                failures.append(
                    f"expected no recommendation, got {len(result.recommendations)}: "
                    + ", ".join(r.id for r in result.recommendations)
                )

            reported_missing = " ".join(result.missing_information).lower()
            for wanted in sorted(case.expected_missing):
                if wanted.lower() not in reported_missing:
                    failures.append(f"missing-information report does not mention '{wanted}'")

            if case.minimum_severity is not None:
                best = max(
                    (r.severity.rank for r in result.recommendations),
                    default=-1,
                )
                if best < case.minimum_severity.rank:
                    failures.append(
                        f"most severe recommendation is below {case.minimum_severity.value}"
                    )

            # The suppression invariant, measured rather than assumed. A contraindication is
            # exempt from every suppression mechanism (ADR-0021); if one is ever suppressed the
            # exemption has been broken somewhere and no accuracy metric would show it.
            for hidden in result.suppressed:
                if hidden.severity is Severity.CONTRAINDICATED:
                    contraindications_suppressed += 1
                    safety.append(f"contraindication '{hidden.id}' was suppressed")

            for recommendation in result.recommendations:
                explainable += 1
                explanation = recommendation.explain()
                if (
                    recommendation.citations
                    and recommendation.provenance
                    and "Sources:" in explanation
                    and "Derivation:" in explanation
                ):
                    explained += 1
                else:
                    failures.append(f"recommendation '{recommendation.id}' is not fully explained")

            if not self._is_repeatable(case, result.recommendations):
                deterministic = False
                safety.append("the same input produced a different decision on re-run")

            shown_counts.append(len(result.recommendations))
            suppressed_total += len(result.suppressed)

            outcomes.append(
                CaseOutcome(
                    case_id=case.case_id,
                    fired_rule_ids=fired,
                    shown=len(result.recommendations),
                    suppressed=len(result.suppressed),
                    latency_ms=latency,
                    failures=tuple(failures),
                    safety_failures=tuple(safety),
                )
            )

        active = {
            rule.rule_id for case in cases for rule in self._rules.active_rules(case.context.as_of)
        }
        uncovered = tuple(sorted(active - covered))

        total_produced = sum(shown_counts) + suppressed_total
        report = DecisionEvalReport(
            case_count=len(cases),
            passed=sum(1 for o in outcomes if o.passed),
            rule_recall=(matched_total / expected_total) if expected_total else 1.0,
            rule_precision=(matched_total / fired_total) if fired_total else 1.0,
            false_positive_count=false_positives,
            mean_alerts_per_case=statistics.fmean(shown_counts),
            max_alerts_in_a_case=max(shown_counts),
            suppression_rate=(suppressed_total / total_produced) if total_produced else 0.0,
            silent_case_rate=sum(1 for c in shown_counts if c == 0) / len(shown_counts),
            rule_coverage=(len(active & covered) / len(active)) if active else 1.0,
            uncovered_rules=uncovered,
            explanation_completeness=(explained / explainable) if explainable else 1.0,
            contraindications_suppressed=contraindications_suppressed,
            deterministic=deterministic,
            p50_latency_ms=statistics.median(latencies),
            p95_latency_ms=sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)],
            outcomes=tuple(outcomes),
        )
        _log.info(
            "decision.evaluated",
            cases=report.case_count,
            passed=report.passed,
            recall=round(report.rule_recall, 3),
            alerts_per_case=round(report.mean_alerts_per_case, 2),
        )
        return report

    def _is_repeatable(self, case: DecisionEvalCase, first: tuple[Any, ...]) -> bool:
        """Whether a second run of the same case produces the same decision.

        Order is part of the comparison. Two runs that surface the same recommendations in a
        different order present a different top alert to a clinician who reads one line, so
        equal-as-sets is not equal enough (ADR-0022).
        """
        second = self._engine.decide(case.context, policy=case.policy).recommendations
        return [(r.id, r.severity, r.rationale) for r in first] == [
            (r.id, r.severity, r.rationale) for r in second
        ]
