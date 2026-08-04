"""The condition expression language.

A small typed AST evaluated by an interpreter. **Never ``eval``** — a knowledge base is a file
an operator edits, and evaluating operator-editable text as Python is remote code execution
with a clinical veneer (docs/design/adr-0019-knowledge-as-data.md).

The language is deliberately small: presence, comparison, membership, temporal windows, trend,
and boolean composition. Anything it cannot express is a signal that a new *operator* is
needed — reviewed once, in code — rather than an escape hatch. That constraint is what keeps
the language reviewable by the clinicians who have to trust the rules written in it.

Every node reports **why** it evaluated as it did, not merely what it evaluated to. A rule that
fires without an explanation is a rule nobody can check, and the explanation is the product
here as much as the boolean.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from cip_decision.domain import ClinicalFact, FactKind, PatientContext

__all__ = [
    "AllOf",
    "AnyOf",
    "Comparison",
    "Condition",
    "ConditionError",
    "Evaluation",
    "HasFact",
    "Not",
    "Operator",
    "TrendDirection",
    "ValueOf",
    "WithinDays",
]


class ConditionError(ValueError):
    """A condition is malformed and cannot be evaluated."""


class Operator(StrEnum):
    """Comparison operators the language supports."""

    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    EQ = "=="
    NEQ = "!="

    def apply(self, left: float, right: float) -> bool:
        return {
            ">": left > right,
            ">=": left >= right,
            "<": left < right,
            "<=": left <= right,
            "==": left == right,
            "!=": left != right,
        }[self.value]


class TrendDirection(StrEnum):
    RISING = "rising"
    FALLING = "falling"
    STABLE = "stable"


@dataclass(frozen=True, slots=True)
class Evaluation:
    """The result of evaluating a condition, and why.

    ``unknown`` is a third outcome alongside true and false, and it is the most important one.
    A rule about potassium on a patient with no potassium recorded is not *false* — it is
    unevaluable, and treating it as false silently converts "we do not know" into "no concern".
    That distinction is what feeds the missing-information detector.
    """

    satisfied: bool
    explanation: str
    unknown: bool = False
    missing: tuple[str, ...] = ()
    matched_facts: tuple[str, ...] = ()

    @property
    def fired(self) -> bool:
        """Whether this condition should cause its rule to fire.

        An unknown condition never fires. The alternative — firing on absence — produces
        recommendations about data nobody has.
        """
        return self.satisfied and not self.unknown

    @staticmethod
    def yes(explanation: str, *, facts: tuple[str, ...] = ()) -> Evaluation:
        return Evaluation(satisfied=True, explanation=explanation, matched_facts=facts)

    @staticmethod
    def no(explanation: str, *, facts: tuple[str, ...] = ()) -> Evaluation:
        return Evaluation(satisfied=False, explanation=explanation, matched_facts=facts)

    @staticmethod
    def unavailable(explanation: str, *, missing: tuple[str, ...]) -> Evaluation:
        return Evaluation(satisfied=False, explanation=explanation, unknown=True, missing=missing)


@runtime_checkable
class Condition(Protocol):
    """Something evaluable against a patient's facts."""

    def evaluate(self, context: PatientContext) -> Evaluation: ...

    def describe(self) -> str:
        """The condition in clinical English, for a rule's explanation."""
        ...


@dataclass(frozen=True, slots=True)
class HasFact:
    """True when the patient has a matching fact."""

    kind: FactKind
    name: str = ""
    code: str | None = None
    active_only: bool = True
    within_days: int | None = None

    def __post_init__(self) -> None:
        if not self.name and not self.code:
            raise ConditionError("HasFact needs a name or a code")

    def evaluate(self, context: PatientContext) -> Evaluation:
        candidates = context.of_kind(self.kind, active_only=self.active_only)
        matched = [f for f in candidates if f.matches(name=self.name or None, code=self.code)]

        if self.within_days is not None:
            dated = [f for f in matched if f.effective is not None]
            undated = [f for f in matched if f.effective is None]
            matched = [
                f
                for f in dated
                if (context.as_of - f.effective).days <= self.within_days  # type: ignore[operator]
            ]
            if not matched and undated:
                # An undated fact cannot satisfy a temporal window, and cannot refute one
                # either. Reporting unknown keeps a rule about "recent" results from firing
                # on a result whose age nobody knows.
                return Evaluation.unavailable(
                    f"{self._label()} is recorded but undated, so its age cannot be checked",
                    missing=(f"date for {self._label()}",),
                )

        if matched:
            return Evaluation.yes(
                f"{self._label()} is present",
                facts=tuple(f.source_ref or f.name for f in matched),
            )
        return Evaluation.no(f"{self._label()} is not present")

    def describe(self) -> str:
        window = f" within {self.within_days} days" if self.within_days is not None else ""
        return f"{self._label()}{window}"

    def _label(self) -> str:
        return f"{self.kind.value} {self.code or self.name}"


@dataclass(frozen=True, slots=True)
class ValueOf:
    """Reads a numeric observation for comparison.

    Not a condition on its own — :class:`Comparison` uses it. Separating the *reference* from
    the *comparison* is what lets a missing value be reported as missing rather than as false.
    """

    name: str
    kind: FactKind = FactKind.OBSERVATION
    within_days: int | None = None

    def resolve(self, context: PatientContext) -> tuple[float | None, ClinicalFact | None, str]:
        """The value, the fact it came from, and why."""
        candidate = context.latest(self.kind, self.name)
        if candidate is None:
            return None, None, f"no {self.name} is recorded"
        if candidate.value is None:
            return None, candidate, f"{self.name} is recorded without a numeric value"
        if self.within_days is not None:
            if candidate.effective is None:
                return None, candidate, f"{self.name} is recorded but undated"
            age = (context.as_of - candidate.effective).days
            if age > self.within_days:
                return (
                    None,
                    candidate,
                    f"the most recent {self.name} is {age} days old, "
                    f"outside the {self.within_days}-day window",
                )
        return candidate.value, candidate, f"{self.name} is {candidate.value}"


@dataclass(frozen=True, slots=True)
class Comparison:
    """Compares an observation against a threshold."""

    subject: ValueOf
    operator: Operator
    threshold: float
    unit: str = ""

    def evaluate(self, context: PatientContext) -> Evaluation:
        value, fact, reason = self.subject.resolve(context)
        if value is None:
            return Evaluation.unavailable(reason, missing=(self.subject.name,))

        # Unit mismatch is refused, not converted. A silent conversion is how a potassium in
        # mmol/L gets compared against a threshold in mg/dL, and the result looks plausible.
        if self.unit and fact is not None and fact.unit and fact.unit.lower() != self.unit.lower():
            return Evaluation.unavailable(
                f"{self.subject.name} is recorded in {fact.unit} but the rule is written for "
                f"{self.unit}; this engine does not convert units",
                missing=(f"{self.subject.name} in {self.unit}",),
            )

        satisfied = self.operator.apply(value, self.threshold)
        rendered = f"{self.subject.name} {value}{' ' + self.unit if self.unit else ''}"
        comparison = f"{self.operator.value} {self.threshold}"
        facts = (fact.source_ref or fact.name,) if fact else ()
        if satisfied:
            return Evaluation.yes(f"{rendered} is {comparison}", facts=facts)
        return Evaluation.no(f"{rendered} is not {comparison}", facts=facts)

    def describe(self) -> str:
        unit = f" {self.unit}" if self.unit else ""
        return f"{self.subject.name} {self.operator.value} {self.threshold}{unit}"


@dataclass(frozen=True, slots=True)
class WithinDays:
    """True when a fact of a kind occurred within a window."""

    kind: FactKind
    name: str
    days: int

    def evaluate(self, context: PatientContext) -> Evaluation:
        return HasFact(kind=self.kind, name=self.name, within_days=self.days).evaluate(context)

    def describe(self) -> str:
        return f"{self.kind.value} {self.name} within {self.days} days"


@dataclass(frozen=True, slots=True)
class Trend:
    """True when a series moves in a direction by more than a relative threshold.

    Requires at least two points. One reading is a value, not a trend, and calling it "stable"
    would be an assertion the data does not support — the same rule Phase 3's lab-trend tool
    follows.
    """

    name: str
    direction: TrendDirection
    minimum_change_ratio: float = 0.05
    minimum_points: int = 2

    def evaluate(self, context: PatientContext) -> Evaluation:
        series = [f for f in context.series(self.name) if f.value is not None]
        if len(series) < self.minimum_points:
            return Evaluation.unavailable(
                f"{self.name} has {len(series)} recorded value(s); a trend needs "
                f"{self.minimum_points}",
                missing=(f"{self.name} series",),
            )

        first, last = series[0], series[-1]
        delta = (last.value or 0.0) - (first.value or 0.0)
        threshold = abs(first.value or 0.0) * self.minimum_change_ratio
        if abs(delta) <= threshold:
            observed = TrendDirection.STABLE
        else:
            observed = TrendDirection.RISING if delta > 0 else TrendDirection.FALLING

        facts = tuple(f.source_ref or f.name for f in series)
        rendered = f"{self.name} moved from {first.value} to {last.value} ({observed.value})"
        if observed is self.direction:
            return Evaluation.yes(rendered, facts=facts)
        return Evaluation.no(f"{rendered}, not {self.direction.value}", facts=facts)

    def describe(self) -> str:
        return f"{self.name} is {self.direction.value}"


@dataclass(frozen=True, slots=True)
class AllOf:
    """Every sub-condition must fire.

    Unknown propagates rather than short-circuiting to false: if one arm is unevaluable, the
    conjunction is unevaluable, and the missing item is reported so the clinician learns what
    would have been needed.
    """

    conditions: tuple[Condition, ...]

    def __post_init__(self) -> None:
        if not self.conditions:
            raise ConditionError("AllOf needs at least one condition")

    def evaluate(self, context: PatientContext) -> Evaluation:
        results = [c.evaluate(context) for c in self.conditions]

        definitely_false = [r for r in results if not r.satisfied and not r.unknown]
        if definitely_false:
            # A definitely-false arm settles it even if another is unknown: the conjunction
            # cannot be true, so the missing data would not change the outcome and asking for
            # it would be noise.
            return Evaluation.no(
                "; ".join(r.explanation for r in definitely_false),
                facts=_collect_facts(results),
            )

        unknowns = [r for r in results if r.unknown]
        if unknowns:
            return Evaluation.unavailable(
                "; ".join(r.explanation for r in unknowns),
                missing=tuple(m for r in unknowns for m in r.missing),
            )

        return Evaluation.yes(
            " and ".join(r.explanation for r in results), facts=_collect_facts(results)
        )

    def describe(self) -> str:
        return " AND ".join(c.describe() for c in self.conditions)


@dataclass(frozen=True, slots=True)
class AnyOf:
    """At least one sub-condition must fire."""

    conditions: tuple[Condition, ...]

    def __post_init__(self) -> None:
        if not self.conditions:
            raise ConditionError("AnyOf needs at least one condition")

    def evaluate(self, context: PatientContext) -> Evaluation:
        results = [c.evaluate(context) for c in self.conditions]

        satisfied = [r for r in results if r.fired]
        if satisfied:
            # One true arm settles a disjunction; unknown arms are irrelevant.
            return Evaluation.yes(
                " or ".join(r.explanation for r in satisfied), facts=_collect_facts(satisfied)
            )

        unknowns = [r for r in results if r.unknown]
        if unknowns:
            return Evaluation.unavailable(
                "; ".join(r.explanation for r in unknowns),
                missing=tuple(m for r in unknowns for m in r.missing),
            )

        return Evaluation.no(
            " and ".join(r.explanation for r in results), facts=_collect_facts(results)
        )

    def describe(self) -> str:
        return " OR ".join(c.describe() for c in self.conditions)


@dataclass(frozen=True, slots=True)
class Not:
    """Negation.

    Unknown does **not** negate to true. "We do not know whether the patient is on an ACE
    inhibitor" must not satisfy "not on an ACE inhibitor" — that is how absence of data
    becomes a positive clinical finding.
    """

    condition: Condition

    def evaluate(self, context: PatientContext) -> Evaluation:
        inner = self.condition.evaluate(context)
        if inner.unknown:
            return Evaluation.unavailable(
                f"cannot determine the absence of: {inner.explanation}", missing=inner.missing
            )
        if inner.satisfied:
            return Evaluation.no(f"not true that {inner.explanation}", facts=inner.matched_facts)
        return Evaluation.yes(f"confirmed absent: {inner.explanation}")

    def describe(self) -> str:
        return f"NOT ({self.condition.describe()})"


@dataclass(frozen=True, slots=True)
class Always:
    """Unconditionally true. For pathway actions that always apply."""

    reason: str = "unconditional"

    def evaluate(self, context: PatientContext) -> Evaluation:  # noqa: ARG002 - protocol
        return Evaluation.yes(self.reason)

    def describe(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class AgeBetween:
    """Patient age in a range. Inclusive lower bound, exclusive upper."""

    minimum: int | None = None
    maximum: int | None = None

    def evaluate(self, context: PatientContext) -> Evaluation:
        if context.age_years is None:
            return Evaluation.unavailable("the patient's age is not recorded", missing=("age",))
        age = context.age_years
        if self.minimum is not None and age < self.minimum:
            return Evaluation.no(f"age {age} is below {self.minimum}")
        if self.maximum is not None and age >= self.maximum:
            return Evaluation.no(f"age {age} is at or above {self.maximum}")
        return Evaluation.yes(f"age {age} is in range")

    def describe(self) -> str:
        low = f"{self.minimum}" if self.minimum is not None else "any"
        high = f"{self.maximum}" if self.maximum is not None else "any"
        return f"age between {low} and {high}"


def _collect_facts(results: list[Evaluation]) -> tuple[str, ...]:
    seen: list[str] = []
    for result in results:
        for fact in result.matched_facts:
            if fact not in seen:
                seen.append(fact)
    return tuple(seen)


#: Fields a temporal condition may reference. Enumerated so the loader can reject an unknown
#: one at load time rather than producing a rule that silently never fires.
TEMPORAL_FIELDS: frozenset[str] = frozenset({"effective", "ended"})


def today_or(context_date: dt.date | None) -> dt.date:
    return context_date or dt.date.today()
