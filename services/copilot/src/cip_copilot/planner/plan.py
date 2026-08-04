"""Plans: what the copilot will do, decided before it does any of it.

A :class:`Plan` is data. Whatever produced it — the rule-based planner here, an LLM planner
later — the executor validates it before running a single step, which is what keeps the cost
and authorisation properties independent of the planner's quality
(docs/design/adr-0009-deterministic-orchestration.md).

Validation is not a formality. It rejects unknown capabilities, argument objects that fail the
tool's schema, plans over the step budget, and duplicate step ids. Every one of those is a
planner bug that would otherwise become a confusing runtime failure halfway through a partly
executed plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = ["Plan", "PlanStep", "PlanValidationError", "StepKind"]


class StepKind(StrEnum):
    """What a step does.

    ``TOOL`` runs a registered tool. ``RETRIEVE`` is called out separately because document
    retrieval is the one capability that always runs for evidence-seeking questions and has
    its own budget and failure semantics.
    """

    TOOL = "tool"
    RETRIEVE = "retrieve"


class PlanValidationError(ValueError):
    """A plan cannot be executed as written."""


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One capability invocation."""

    step_id: str
    kind: StepKind
    capability: str
    arguments: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    """Why the planner chose this. Surfaced in the explanation — "it looked up your
    medications because you asked about an interaction" is the difference between a trace and
    an explanation."""
    optional: bool = False
    """A failure here degrades the answer rather than ending the run."""

    def __post_init__(self) -> None:
        if not self.step_id.strip():
            raise ValueError("PlanStep.step_id must not be empty")
        if not self.capability.strip():
            raise ValueError("PlanStep.capability must not be empty")


@dataclass(frozen=True, slots=True)
class Plan:
    """An ordered set of steps, plus why this shape was chosen."""

    steps: tuple[PlanStep, ...]
    intent: str = "unknown"
    confidence: float = 0.0
    rationale: str = ""
    needs_clarification: str | None = None
    """Set when the question cannot be planned at all — an unresolved pronoun with no
    referent, a patient-scoped question with no patient. The orchestrator asks rather than
    guessing, because a confidently wrong patient is the worst outcome available."""

    @property
    def is_empty(self) -> bool:
        return not self.steps

    def estimated_cost_ms(self, cost_by_capability: dict[str, float]) -> float:
        return sum(cost_by_capability.get(step.capability, 5.0) for step in self.steps)

    def describe(self) -> str:
        return "; ".join(f"{s.step_id}:{s.capability}" for s in self.steps)


def validate_plan(plan: Plan, *, registry: Any, max_steps: int = 8) -> None:
    """Check a plan against the registry before anything runs.

    Raises :class:`PlanValidationError` on the first problem. Fail-fast rather than
    best-effort: a partially executed plan has already read PHI and spent latency, and
    discovering step 4 is malformed at that point leaves the run in a state nobody designed.
    """
    if plan.needs_clarification:
        return

    if len(plan.steps) > max_steps:
        raise PlanValidationError(
            f"Plan has {len(plan.steps)} steps, over the budget of {max_steps}"
        )

    seen: set[str] = set()
    for step in plan.steps:
        if step.step_id in seen:
            raise PlanValidationError(f"Duplicate step id '{step.step_id}'")
        seen.add(step.step_id)

        if step.kind is not StepKind.TOOL:
            continue

        try:
            tool = registry.get(step.capability)
        except Exception as exc:
            raise PlanValidationError(
                f"Step '{step.step_id}' names unknown capability '{step.capability}'"
            ) from exc

        # Validate arguments here, not at execution time, so a malformed plan is rejected
        # whole rather than discovered midway.
        from cip_copilot.tools.base import ToolError, validate_arguments

        try:
            validate_arguments(tool.spec, step.arguments)
        except ToolError as exc:
            raise PlanValidationError(
                f"Step '{step.step_id}' arguments are invalid for '{step.capability}': {exc}"
            ) from exc
