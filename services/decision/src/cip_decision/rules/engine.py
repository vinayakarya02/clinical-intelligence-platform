"""Rule evaluation.

A rule is a condition, an effect, and its provenance. The engine evaluates every rule whose
version is active, records **why each one did or did not fire**, and turns the ones that fired
into recommendations.

Two properties are load-bearing.

**Non-firing is explained too.** A trace that only records what fired cannot answer "why did
it not warn me", which is the question asked after an incident. The full trace is the product.

**Unknown is not false.** A rule about potassium on a patient with no potassium recorded is
unevaluable, not satisfied-as-false. Those rules feed the missing-information detector instead
of silently contributing nothing.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from typing import Any

from cip_core.logging import get_logger
from cip_decision.domain import (
    Citation,
    EvidenceQuality,
    PatientContext,
    ProvenanceLink,
    Recommendation,
    RecommendationKind,
    Severity,
)
from cip_decision.rules.ast import Condition, Evaluation

__all__ = ["ClinicalRule", "RuleEngine", "RuleOutcome", "RuleTrace"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ClinicalRule:
    """One clinical rule.

    Constructed by the knowledge loader from a versioned, cited artifact — never written in
    Python (docs/design/adr-0019-knowledge-as-data.md). The dataclass exists so the engine has
    something typed to work with, not so rules can be authored here.
    """

    rule_id: str
    version: str
    title: str
    condition: Condition
    recommendation_summary: str
    severity: Severity
    evidence_quality: EvidenceQuality
    citations: tuple[Citation, ...]

    kind: RecommendationKind = RecommendationKind.ALERT
    detail: str = ""
    effective_from: dt.date | None = None
    effective_until: dt.date | None = None
    guideline_id: str = ""
    tags: tuple[str, ...] = ()
    #: Recommendations this rule's output supersedes. Used by the deduplication stage, so two
    #: rules reaching the same clinical concern produce one alert with two supports.
    supersedes: tuple[str, ...] = ()
    #: A clinical concern identifier shared by rules that mean the same thing. Two rules with
    #: the same concern deduplicate; two rules without one never do.
    concern: str = ""
    #: Which way this rule points about its subject — ``toward``, ``away``, or unstated.
    #: Declared by the knowledge author, because inferring it from the recommendation text
    #: produced false conflicts (see cip_decision.contradiction).
    direction: str = ""

    def __post_init__(self) -> None:
        if not self.citations:
            raise ValueError(
                f"Rule '{self.rule_id}' has no citation. An uncited clinical rule cannot be "
                "reviewed or defended."
            )
        if (
            self.effective_until
            and self.effective_from
            and self.effective_until < self.effective_from
        ):
            raise ValueError(f"Rule '{self.rule_id}' expires before it becomes effective")

    def is_active(self, on: dt.date) -> bool:
        """Whether this rule version applies on ``on``.

        Dates rather than a boolean flag: a guideline supersession has a date, and a knowledge
        base that can only say "enabled" cannot represent "this became the standard in March".
        """
        if self.effective_from and on < self.effective_from:
            return False
        return not (self.effective_until and on > self.effective_until)

    @property
    def key(self) -> str:
        return f"{self.rule_id}@{self.version}"


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """What one rule concluded, and why."""

    rule: ClinicalRule
    evaluation: Evaluation
    duration_ms: float = 0.0

    @property
    def fired(self) -> bool:
        return self.evaluation.fired

    @property
    def unknown(self) -> bool:
        return self.evaluation.unknown

    def describe(self) -> str:
        verdict = "fired" if self.fired else ("unevaluable" if self.unknown else "did not fire")
        return f"{self.rule.key} {verdict}: {self.evaluation.explanation}"


@dataclass(frozen=True, slots=True)
class RuleTrace:
    """Every rule the engine considered, and what happened to it."""

    outcomes: tuple[RuleOutcome, ...] = ()
    skipped_inactive: tuple[str, ...] = ()
    duration_ms: float = 0.0

    @property
    def fired(self) -> tuple[RuleOutcome, ...]:
        return tuple(o for o in self.outcomes if o.fired)

    @property
    def unevaluable(self) -> tuple[RuleOutcome, ...]:
        """Rules that could not be evaluated for want of data.

        The input to the missing-information detector. These are the cases where the system
        *would* have had something to say given one more fact.
        """
        return tuple(o for o in self.outcomes if o.unknown)

    def missing_information(self) -> tuple[str, ...]:
        """Everything that, if known, would let a rule be evaluated."""
        seen: list[str] = []
        for outcome in self.unevaluable:
            for item in outcome.evaluation.missing:
                if item not in seen:
                    seen.append(item)
        return tuple(seen)

    def to_json(self) -> dict[str, Any]:
        return {
            "considered": len(self.outcomes),
            "fired": [o.rule.key for o in self.fired],
            "unevaluable": [o.rule.key for o in self.unevaluable],
            "skipped_inactive": list(self.skipped_inactive),
            "missing_information": list(self.missing_information()),
            "duration_ms": round(self.duration_ms, 3),
        }

    def render(self) -> str:
        """The full trace, including rules that did not fire.

        A trace that omits non-firing rules cannot answer "why did it not warn me", which is
        the question asked after an incident.
        """
        return "\n".join(o.describe() for o in self.outcomes)


class RuleEngine:
    """Evaluates a rule set against a patient.

    Stateless and deterministic: the same facts and the same rule set produce the same
    outcomes, in the same order, every time. That is what makes a rule set clinically
    reviewable — a reviewer can reason about what it will do.
    """

    def __init__(self, rules: list[ClinicalRule] | None = None) -> None:
        self._rules: dict[str, ClinicalRule] = {}
        for rule in rules or []:
            self.register(rule)

    def register(self, rule: ClinicalRule) -> None:
        """Add a rule. A duplicate ``rule_id@version`` is refused.

        Silent replacement would make which version is active depend on load order, and a
        knowledge base whose behaviour depends on file iteration order is not reviewable.
        """
        if rule.key in self._rules:
            raise ValueError(f"Rule {rule.key} is already registered")
        self._rules[rule.key] = rule

    def active_rules(self, on: dt.date) -> tuple[ClinicalRule, ...]:
        """Rules in force on a date, ordered deterministically."""
        return tuple(
            sorted(
                (r for r in self._rules.values() if r.is_active(on)),
                key=lambda r: (r.rule_id, r.version),
            )
        )

    def evaluate(self, context: PatientContext, *, tags: frozenset[str] | None = None) -> RuleTrace:
        """Evaluate every applicable rule, recording all outcomes."""
        started = time.perf_counter()
        outcomes: list[RuleOutcome] = []
        skipped: list[str] = []

        for rule in sorted(self._rules.values(), key=lambda r: (r.rule_id, r.version)):
            if not rule.is_active(context.as_of):
                skipped.append(rule.key)
                continue
            if tags is not None and not (set(rule.tags) & tags):
                continue

            rule_started = time.perf_counter()
            try:
                evaluation = rule.condition.evaluate(context)
            except Exception as exc:
                # A broken rule must not take down the evaluation of every other rule. It is
                # recorded as unevaluable — which routes it to missing-information rather than
                # letting it vanish, because a rule that silently errors is a rule that
                # silently stopped protecting anyone.
                _log.error("rules.evaluation_failed", rule=rule.key, error=type(exc).__name__)
                evaluation = Evaluation.unavailable(
                    f"rule {rule.key} could not be evaluated: {type(exc).__name__}",
                    missing=(f"working rule {rule.rule_id}",),
                )
            outcomes.append(
                RuleOutcome(
                    rule=rule,
                    evaluation=evaluation,
                    duration_ms=(time.perf_counter() - rule_started) * 1000,
                )
            )

        trace = RuleTrace(
            outcomes=tuple(outcomes),
            skipped_inactive=tuple(skipped),
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        _log.debug(
            "rules.evaluated",
            considered=len(trace.outcomes),
            fired=len(trace.fired),
            unevaluable=len(trace.unevaluable),
        )
        return trace

    @staticmethod
    def to_recommendations(trace: RuleTrace, context: PatientContext) -> tuple[Recommendation, ...]:
        """Turn fired rules into recommendations.

        Each carries the rule's citations and a provenance chain reaching back through the
        rule to the facts that triggered it, so ``Recommendation``'s constructor invariant is
        satisfied by construction rather than by remembering.
        """
        recommendations: list[Recommendation] = []
        for outcome in trace.fired:
            rule = outcome.rule
            provenance = [
                ProvenanceLink(
                    kind="rule",
                    identifier=rule.key,
                    label=rule.title,
                    detail=rule.condition.describe(),
                )
            ]
            if rule.guideline_id:
                provenance.insert(
                    0,
                    ProvenanceLink(
                        kind="guideline", identifier=rule.guideline_id, label="source guideline"
                    ),
                )
            for fact in outcome.evaluation.matched_facts:
                provenance.append(
                    ProvenanceLink(kind="fact", identifier=fact, label="triggering fact")
                )

            recommendations.append(
                Recommendation(
                    id=f"rec:{rule.rule_id}:{context.patient_id}",
                    kind=rule.kind,
                    summary=rule.recommendation_summary,
                    severity=rule.severity,
                    evidence_quality=rule.evidence_quality,
                    citations=rule.citations,
                    provenance=tuple(provenance),
                    detail=rule.detail,
                    rationale=outcome.evaluation.explanation,
                    patient_id=context.patient_id,
                    source_rule_ids=(rule.key,),
                    triggering_facts=outcome.evaluation.matched_facts,
                    metadata={
                        k: v
                        for k, v in (
                            ("concern", rule.concern),
                            ("direction", rule.direction),
                        )
                        if v
                    },
                )
            )
        return tuple(recommendations)
