"""Care pathway application.

Modelled on FHIR ``PlanDefinition`` (docs/design/adr-0023-fhir-clinical-reasoning.md): a tree
of actions, each with an applicability condition, applied to a patient to produce a concrete
plan — FHIR's ``$apply`` semantics.

Two decisions carry the module.

**Applicability conditions use the same rules engine as everything else.** A pathway condition
and a standalone rule are the same kind of thing, so they get the same evaluator, the same
trace, and the same explanation. A second condition language would be a second thing to review.

**Not-applicable actions are retained, with their reason.** Dropping them makes the produced
plan indistinguishable from one where the action was never considered, and "we checked and it
does not apply" is clinically different from silence.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from cip_core.logging import get_logger
from cip_decision.domain import Citation, PatientContext
from cip_decision.rules.ast import Condition, Evaluation

__all__ = [
    "Applicability",
    "AppliedAction",
    "AppliedPathway",
    "CarePathway",
    "PathwayAction",
    "PathwayEngine",
    "PathwayStage",
]

_log = get_logger(__name__)


class PathwayStage(StrEnum):
    """Where in the care journey an action sits.

    Ordered, so an applied pathway renders in clinical sequence rather than in declaration
    order — a plan that lists discharge before investigation is a plan nobody can follow.
    """

    INVESTIGATION = "investigation"
    DIAGNOSIS = "diagnosis"
    TREATMENT = "treatment"
    MONITORING = "monitoring"
    FOLLOW_UP = "follow_up"
    DISCHARGE = "discharge"

    @property
    def order(self) -> int:
        return {
            "diagnosis": 0,
            "investigation": 1,
            "treatment": 2,
            "monitoring": 3,
            "follow_up": 4,
            "discharge": 5,
        }[self.value]


class Applicability(StrEnum):
    """Whether an action applies to this patient."""

    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"
    UNDETERMINED = "undetermined"
    """The condition could not be evaluated. Distinct from not-applicable: it means a fact is
    missing, and the plan says which one."""


@dataclass(frozen=True, slots=True)
class PathwayAction:
    """One step, possibly with children."""

    action_id: str
    title: str
    stage: PathwayStage
    description: str = ""
    condition: Condition | None = None
    children: tuple[PathwayAction, ...] = ()

    def walk(self) -> tuple[PathwayAction, ...]:
        """This action and every descendant, depth-first."""
        found: list[PathwayAction] = [self]
        for child in self.children:
            found.extend(child.walk())
        return tuple(found)


@dataclass(frozen=True, slots=True)
class CarePathway:
    """A versioned, cited pathway definition."""

    pathway_id: str
    version: str
    title: str
    actions: tuple[PathwayAction, ...]
    citations: tuple[Citation, ...]
    trigger: Condition | None = None
    guideline_id: str = ""
    effective_from: dt.date | None = None
    effective_until: dt.date | None = None

    def __post_init__(self) -> None:
        if not self.actions:
            raise ValueError(f"Pathway '{self.pathway_id}' has no actions")
        if not self.citations:
            raise ValueError(f"Pathway '{self.pathway_id}' has no citation")
        ids = [a.action_id for root in self.actions for a in root.walk()]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            # Duplicate ids would make an applied action ambiguous to reference, which breaks
            # both the audit trail and any downstream system tracking completion.
            raise ValueError(
                f"Pathway '{self.pathway_id}' has duplicate action ids: {sorted(duplicates)}"
            )

    def is_active(self, on: dt.date) -> bool:
        if self.effective_from and on < self.effective_from:
            return False
        return not (self.effective_until and on > self.effective_until)

    @property
    def key(self) -> str:
        return f"{self.pathway_id}@{self.version}"


@dataclass(frozen=True, slots=True)
class AppliedAction:
    """One action, resolved against a patient."""

    action: PathwayAction
    applicability: Applicability
    reason: str
    missing: tuple[str, ...] = ()
    depth: int = 0

    @property
    def is_applicable(self) -> bool:
        return self.applicability is Applicability.APPLICABLE

    def render(self) -> str:
        marker = {"applicable": "[x]", "not_applicable": "[ ]", "undetermined": "[?]"}[
            self.applicability.value
        ]
        indent = "  " * self.depth
        return f"{indent}{marker} {self.action.title} — {self.reason}"


@dataclass(frozen=True, slots=True)
class AppliedPathway:
    """A pathway resolved against one patient."""

    pathway: CarePathway
    actions: tuple[AppliedAction, ...] = ()
    triggered: bool = True
    trigger_reason: str = ""

    @property
    def applicable(self) -> tuple[AppliedAction, ...]:
        return tuple(a for a in self.actions if a.is_applicable)

    @property
    def undetermined(self) -> tuple[AppliedAction, ...]:
        return tuple(a for a in self.actions if a.applicability is Applicability.UNDETERMINED)

    def missing_information(self) -> tuple[str, ...]:
        seen: list[str] = []
        for applied in self.undetermined:
            for item in applied.missing:
                if item not in seen:
                    seen.append(item)
        return tuple(seen)

    def by_stage(self) -> dict[str, tuple[AppliedAction, ...]]:
        """Applicable actions grouped by stage, in clinical order."""
        grouped: dict[str, list[AppliedAction]] = {}
        for applied in sorted(self.applicable, key=lambda a: a.action.stage.order):
            grouped.setdefault(applied.action.stage.value, []).append(applied)
        return {stage: tuple(items) for stage, items in grouped.items()}

    def render(self) -> str:
        lines = [f"{self.pathway.title} ({self.pathway.key})"]
        if not self.triggered:
            lines.append(f"  not triggered: {self.trigger_reason}")
            return "\n".join(lines)
        lines.extend(a.render() for a in self.actions)
        if self.missing_information():
            lines.append("  unresolved: " + ", ".join(self.missing_information()))
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "pathway": self.pathway.key,
            "triggered": self.triggered,
            "applicable": [a.action.action_id for a in self.applicable],
            "not_applicable": [
                a.action.action_id
                for a in self.actions
                if a.applicability is Applicability.NOT_APPLICABLE
            ],
            "undetermined": [a.action.action_id for a in self.undetermined],
            "missing_information": list(self.missing_information()),
            "stages": {k: [a.action.action_id for a in v] for k, v in self.by_stage().items()},
        }


class PathwayEngine:
    """Applies pathways to patients."""

    def __init__(self, pathways: tuple[CarePathway, ...] = ()) -> None:
        self._pathways = {p.pathway_id: p for p in pathways}

    def pathway_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._pathways))

    def apply(self, pathway_id: str, context: PatientContext) -> AppliedPathway:
        """Resolve every action in a pathway against a patient."""
        pathway = self._pathways.get(pathway_id)
        if pathway is None:
            raise KeyError(f"No pathway '{pathway_id}' is configured")
        if not pathway.is_active(context.as_of):
            return AppliedPathway(
                pathway=pathway,
                triggered=False,
                trigger_reason=f"pathway version {pathway.version} is not in effect",
            )

        if pathway.trigger is not None:
            trigger = pathway.trigger.evaluate(context)
            if not trigger.fired:
                # An untriggered pathway returns with its reason rather than an empty result:
                # "the trigger did not fire because potassium is 4.1" is useful; silence is not.
                return AppliedPathway(
                    pathway=pathway, triggered=False, trigger_reason=trigger.explanation
                )

        applied: list[AppliedAction] = []
        for root in pathway.actions:
            applied.extend(self._apply_action(root, context, depth=0))

        result = AppliedPathway(pathway=pathway, actions=tuple(applied), triggered=True)
        _log.debug(
            "pathway.applied",
            pathway=pathway.key,
            applicable=len(result.applicable),
            undetermined=len(result.undetermined),
        )
        return result

    def apply_triggered(self, context: PatientContext) -> tuple[AppliedPathway, ...]:
        """Every pathway whose trigger fires for this patient."""
        results = [self.apply(pid, context) for pid in self.pathway_ids()]
        return tuple(r for r in results if r.triggered)

    def _apply_action(
        self, action: PathwayAction, context: PatientContext, *, depth: int
    ) -> list[AppliedAction]:
        """Resolve one action and its children."""
        if action.condition is None:
            evaluation = Evaluation.yes("no applicability condition")
        else:
            evaluation = action.condition.evaluate(context)

        if evaluation.unknown:
            applicability = Applicability.UNDETERMINED
        elif evaluation.satisfied:
            applicability = Applicability.APPLICABLE
        else:
            applicability = Applicability.NOT_APPLICABLE

        resolved = [
            AppliedAction(
                action=action,
                applicability=applicability,
                reason=evaluation.explanation,
                missing=evaluation.missing,
                depth=depth,
            )
        ]

        # Children of a non-applicable parent are not evaluated. Evaluating them would
        # produce actions inside a branch the patient is not on, which reads as a
        # recommendation rather than as dead structure.
        if applicability is Applicability.APPLICABLE:
            for child in action.children:
                resolved.extend(self._apply_action(child, context, depth=depth + 1))
        return resolved
