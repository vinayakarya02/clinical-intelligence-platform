"""Risk stratification.

A generic engine over configured point scores. Every model is data: components, points,
conditions, bands, and citations all come from the knowledge base
(docs/design/adr-0019-knowledge-as-data.md).

Only **published point scores** are supported. A model-derived risk estimate about a patient is
a regulated clinical claim rather than a calculation, so there is no path here for a fitted
model — that is a deliberate exclusion, recorded in the safety case.

Every score reports which components contributed and which did not. A number a clinician
cannot decompose is a number they cannot check, and an unexplained risk score is the kind of
output that gets acted on without scrutiny.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cip_core.logging import get_logger
from cip_decision.domain import Citation, PatientContext
from cip_decision.rules.ast import Condition, Evaluation

__all__ = ["RiskComponent", "RiskModel", "RiskResult", "RiskScorer"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RiskComponent:
    """One criterion contributing points."""

    component_id: str
    label: str
    points: int
    condition: Condition

    def evaluate(self, context: PatientContext) -> tuple[int, Evaluation]:
        """Points contributed, and why."""
        evaluation = self.condition.evaluate(context)
        return (self.points if evaluation.fired else 0), evaluation


@dataclass(frozen=True, slots=True)
class RiskBand:
    """A score threshold and what it conventionally means."""

    at_least: int
    band: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class RiskModel:
    """A published point score."""

    model_id: str
    version: str
    title: str
    components: tuple[RiskComponent, ...]
    citations: tuple[Citation, ...]
    bands: tuple[RiskBand, ...] = ()
    guideline_id: str = ""
    applies_when: Condition | None = None
    """The population this score is defined for.

    CHA2DS2-VASc estimates stroke risk *in non-valvular atrial fibrillation*. Computing and
    banding it for a patient without atrial fibrillation produces a clinically meaningless
    number that looks authoritative — and one that could drive inappropriate anticoagulation.
    A model without a stated population is scored for everyone, which is correct only for
    models that genuinely are."""

    def __post_init__(self) -> None:
        if not self.components:
            raise ValueError(f"Risk model '{self.model_id}' has no components")
        if not self.citations:
            raise ValueError(
                f"Risk model '{self.model_id}' has no citation. An uncited score cannot be "
                "reviewed or defended."
            )

    @property
    def maximum(self) -> int:
        return sum(c.points for c in self.components)


@dataclass(frozen=True, slots=True)
class RiskResult:
    """A computed score, fully decomposed."""

    model: RiskModel
    score: int
    applicable: bool = True
    """False when the model's population precondition is not met. An inapplicable model
    reports *no score at all* rather than zero — zero is a value a clinician can act on."""
    inapplicable_reason: str = ""
    contributing: tuple[tuple[str, int, str], ...] = ()
    absent: tuple[tuple[str, str], ...] = ()
    unevaluable: tuple[tuple[str, str], ...] = ()
    unevaluable_points: int = 0
    """Points that could not be evaluated. Carried explicitly rather than re-derived: the
    earlier version matched component *labels* against component *ids*, so it never matched
    and every incomplete score reported an upper bound equal to its lower bound — the one
    number that tells a clinician how much risk might be hidden."""
    band: str = ""
    band_note: str = ""

    @property
    def is_complete(self) -> bool:
        """Whether every component could be evaluated.

        An incomplete score is a **lower bound**, not a score. Reporting it as a score would
        understate risk for exactly the patients whose records are thin — which are often the
        patients about whom least is known and most is at stake.
        """
        return not self.unevaluable

    @property
    def maximum_possible(self) -> int:
        """The highest this score could reach given what is unknown."""
        return self.score + self.unevaluable_points

    def explain(self) -> str:
        if not self.applicable:
            return (
                f"{self.model.title}: not scored — {self.inapplicable_reason}. "
                "This score is defined for a specific population and is not meaningful "
                "outside it."
            )
        lines = [f"{self.model.title}: {self.score} of a possible {self.model.maximum}"]
        if self.band:
            lines.append(f"Band: {self.band}" + (f" — {self.band_note}" if self.band_note else ""))
        if self.contributing:
            lines.append("Contributing:")
            lines.extend(f"  +{points} {label} ({why})" for label, points, why in self.contributing)
        if self.absent:
            lines.append("Not contributing:")
            lines.extend(f"  0 {label} ({why})" for label, why in self.absent)
        if self.unevaluable:
            lines.append(
                f"Incomplete — this is a lower bound. Could reach {self.maximum_possible} if:"
            )
            lines.extend(f"  ? {label} ({why})" for label, why in self.unevaluable)
        lines.append("Sources: " + "; ".join(c.render() for c in self.model.citations))
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        if not self.applicable:
            return {
                "model": f"{self.model.model_id}@{self.model.version}",
                "applicable": False,
                "reason": self.inapplicable_reason,
            }
        return {
            "model": f"{self.model.model_id}@{self.model.version}",
            "applicable": True,
            "score": self.score,
            "maximum": self.model.maximum,
            "band": self.band,
            "complete": self.is_complete,
            "lower_bound_only": not self.is_complete,
            "contributing": [
                {"label": label, "points": points} for label, points, _ in self.contributing
            ],
            "unevaluable": [label for label, _ in self.unevaluable],
        }


class RiskScorer:
    """Computes configured risk models against a patient."""

    def __init__(self, models: tuple[RiskModel, ...] = ()) -> None:
        self._models = {m.model_id: m for m in models}

    def models(self) -> tuple[str, ...]:
        return tuple(sorted(self._models))

    def score(self, model_id: str, context: PatientContext) -> RiskResult:
        """Compute one model, decomposed."""
        model = self._models.get(model_id)
        if model is None:
            raise KeyError(f"No risk model '{model_id}' is configured")

        if model.applies_when is not None:
            applicability = model.applies_when.evaluate(context)
            if not applicability.fired:
                # Not scored, not scored-as-zero. A CHA2DS2-VASc of 0 for a patient without
                # atrial fibrillation reads as "low stroke risk" when the truth is "this
                # score does not apply here".
                _log.debug("risk.not_applicable", model=model_id, reason=applicability.explanation)
                return RiskResult(
                    model=model,
                    score=0,
                    applicable=False,
                    inapplicable_reason=applicability.explanation,
                )

        total = 0
        unevaluable_points = 0
        contributing: list[tuple[str, int, str]] = []
        absent: list[tuple[str, str]] = []
        unevaluable: list[tuple[str, str]] = []

        for component in model.components:
            points, evaluation = component.evaluate(context)
            if evaluation.unknown:
                unevaluable.append((component.label, evaluation.explanation))
                unevaluable_points += component.points
            elif points:
                total += points
                contributing.append((component.label, points, evaluation.explanation))
            else:
                absent.append((component.label, evaluation.explanation))

        band, note = self._band_for(model, total)
        result = RiskResult(
            model=model,
            score=total,
            contributing=tuple(contributing),
            absent=tuple(absent),
            unevaluable=tuple(unevaluable),
            unevaluable_points=unevaluable_points,
            band=band,
            band_note=note,
        )
        _log.debug(
            "risk.scored",
            model=model_id,
            score=total,
            complete=result.is_complete,
        )
        return result

    def score_all(self, context: PatientContext) -> tuple[RiskResult, ...]:
        """Every configured model. Inapplicable ones are returned, marked, not silently
        dropped — "we checked and it does not apply" differs from silence."""
        return tuple(self.score(model_id, context) for model_id in self.models())

    def applicable_results(self, context: PatientContext) -> tuple[RiskResult, ...]:
        """Only models whose population precondition is met."""
        return tuple(r for r in self.score_all(context) if r.applicable)

    @staticmethod
    def _band_for(model: RiskModel, score: int) -> tuple[str, str]:
        """The highest band whose threshold the score meets."""
        applicable = [b for b in model.bands if score >= b.at_least]
        if not applicable:
            return "", ""
        best = max(applicable, key=lambda b: b.at_least)
        return best.band, best.note
