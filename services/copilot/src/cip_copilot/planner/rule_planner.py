"""Rule-based clinical planner.

Maps a question to a plan using lexical markers, the same technique Phase 2's query router
uses and for the same reasons: clinical question shapes are a small stable set, the rules are
inspectable and free, and a plan is a value a test can assert on.

The planner composes rather than classifies-then-dispatches. "Is his potassium rising on
spironolactone?" is simultaneously a lab-trend question, a medication question, and an
interaction question, and the right plan runs all three. A single-label classifier would pick
one and answer two thirds of the question without saying so.

Where it cannot proceed it says so. A patient-scoped question with no patient in scope
produces ``needs_clarification`` rather than a plan against a guessed patient — the one
failure mode with no acceptable recovery.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from cip_copilot.domain import CopilotQuestion
from cip_copilot.planner.plan import Plan, PlanStep, StepKind
from cip_core.logging import get_logger

__all__ = ["ClinicalRulePlanner", "Planner", "PlanningRules"]

_log = get_logger(__name__)


@runtime_checkable
class Planner(Protocol):
    """Turns a question into a plan."""

    @property
    def name(self) -> str: ...

    def plan(
        self, question: CopilotQuestion, *, resolved_text: str, available: tuple[str, ...]
    ) -> Plan: ...


#: Lexical markers per capability. Weighted by how strongly each discriminates: "interacts
#: with" nearly always signals an interaction question, while "what" signals nothing.
_MARKERS: dict[str, tuple[tuple[re.Pattern[str], float], ...]] = {
    "medication_lookup": (
        (re.compile(r"\b(?:medication|drug|prescri|taking|dose|dosage)\w*", re.I), 2.0),
        (re.compile(r"\bon what\b", re.I), 1.0),
    ),
    "diagnosis_lookup": (
        (re.compile(r"\b(?:diagnos|condition|problem|history of|comorbid)\w*", re.I), 2.0),
    ),
    "lab_trend": (
        (
            re.compile(r"\b(?:trend|rising|falling|improving|worsening|over time|serial)\w*", re.I),
            2.5,
        ),
        (
            re.compile(
                r"\b(?:potassium|sodium|creatinine|troponin|hemoglobin|glucose|inr|"
                r"platelet|albumin)\b",
                re.I,
            ),
            2.0,
        ),
    ),
    "drug_interaction_check": (
        (re.compile(r"\binteract\w*", re.I), 3.0),
        (re.compile(r"\bcontraindicat\w*", re.I), 3.0),
        (re.compile(r"\b(?:safe (?:to|with)|combin\w+|together with)\b", re.I), 2.0),
    ),
    "timeline_reconstruct": (
        (re.compile(r"\b(?:timeline|chronolog\w+|progression|course|history)\b", re.I), 2.5),
        (re.compile(r"\bwhat happened\b", re.I), 2.5),
    ),
    "risk_score": ((re.compile(r"\b(?:risk score|chads|cha2ds2|stroke risk|score)\b", re.I), 2.5),),
    "guideline_lookup": (
        (re.compile(r"\b(?:guideline|recommend\w*|standard of care|protocol)\b", re.I), 2.5),
    ),
    "graph_traversal": (
        (
            re.compile(r"\b(?:related to|linked to|associated with|cause[sd]?|leads? to)\b", re.I),
            2.0,
        ),
    ),
    "patient_lookup": ((re.compile(r"\b(?:how old|age|demograph\w*|who is)\b", re.I), 2.0),),
}

#: Analytes the lab-trend step can be pointed at. A trend step needs to know *which* lab, and
#: guessing would produce a confident trend for the wrong analyte.
_ANALYTES = (
    "potassium",
    "sodium",
    "creatinine",
    "troponin",
    "hemoglobin",
    "glucose",
    "inr",
    "platelet",
    "albumin",
)

#: Capabilities that cannot run without a patient in scope.
_PATIENT_SCOPED = frozenset(
    {
        "patient_lookup",
        "diagnosis_lookup",
        "medication_lookup",
        "lab_trend",
        "timeline_reconstruct",
        "risk_score",
    }
)


@dataclass(frozen=True, slots=True)
class PlanningRules:
    """Tunable thresholds for planning."""

    marker_threshold: float = 2.0
    """Minimum score for a capability to earn a step. Below it the marker is likely
    incidental — "history" appears in "history of present illness"."""

    max_steps: int = 6
    always_retrieve: bool = True
    """Document search runs for every evidence-seeking question. Structured tools answer what
    is coded; the narrative holds what is not, and a question answered only from codes misses
    everything a clinician wrote."""


class ClinicalRulePlanner:
    """Composes a plan from lexical markers over the question."""

    def __init__(self, rules: PlanningRules | None = None) -> None:
        self._rules = rules or PlanningRules()

    @property
    def name(self) -> str:
        return "clinical-rules"

    def plan(
        self, question: CopilotQuestion, *, resolved_text: str, available: tuple[str, ...]
    ) -> Plan:
        """Build a plan from the resolved question text."""
        text = resolved_text or question.text
        offered = set(available)

        scores = self._score(text)
        chosen = [
            capability
            for capability, score in sorted(scores.items(), key=lambda kv: -kv[1])
            if score >= self._rules.marker_threshold and capability in offered
        ]

        patient_needed = [c for c in chosen if c in _PATIENT_SCOPED]
        if patient_needed and question.patient_id is None:
            return Plan(
                steps=(),
                intent="patient_scoped",
                confidence=0.0,
                rationale="The question is about a specific patient's record.",
                needs_clarification=(
                    "Which patient is this about? The question asks for "
                    f"{_readable(patient_needed[0])}, which is specific to one patient's record."
                ),
            )

        steps: list[PlanStep] = []
        for index, capability in enumerate(chosen, start=1):
            arguments = self._arguments(capability, question, text)
            if arguments is None:
                continue
            steps.append(
                PlanStep(
                    step_id=f"s{index}",
                    kind=StepKind.TOOL,
                    capability=capability,
                    arguments=arguments,
                    rationale=f"the question asks about {_readable(capability)}",
                    optional=True,
                )
            )
            if len(steps) >= self._rules.max_steps - 1:
                break

        if self._rules.always_retrieve and "document_search" in offered:
            steps.append(
                PlanStep(
                    step_id=f"s{len(steps) + 1}",
                    kind=StepKind.TOOL,
                    capability="document_search",
                    arguments={"query": text, "top_k": 8},
                    rationale="clinical narrative may hold what the coded record does not",
                    optional=False,
                )
            )

        top = max(scores.values(), default=0.0)
        runner_up = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0.0
        confidence = _confidence(top, runner_up, self._rules.marker_threshold)

        plan = Plan(
            steps=tuple(steps),
            intent=chosen[0] if chosen else "general",
            confidence=confidence,
            rationale=(
                f"matched {', '.join(chosen)}" if chosen else "no specific capability matched"
            ),
        )
        _log.debug(
            "planner.planned",
            planner=self.name,
            steps=len(plan.steps),
            intent=plan.intent,
            confidence=plan.confidence,
        )
        return plan

    @staticmethod
    def _score(text: str) -> dict[str, float]:
        """Score every capability against the question text."""
        scores: dict[str, float] = {}
        for capability, markers in _MARKERS.items():
            total = 0.0
            for pattern, weight in markers:
                # Count distinct matched markers, capped at two, so a question repeating
                # synonyms cannot let one capability crowd out every other — the same
                # correction Phase 2's router needed.
                distinct = len({m.lower() for m in pattern.findall(text)})
                if distinct:
                    total += weight * min(distinct, 2)
            if total:
                scores[capability] = total
        return scores

    @staticmethod
    def _arguments(capability: str, question: CopilotQuestion, text: str) -> dict[str, Any] | None:
        """Build a step's arguments, or ``None`` if they cannot be determined.

        Returning ``None`` drops the step. A step whose arguments had to be guessed would run
        against the wrong analyte or the wrong concept and return confident, irrelevant
        evidence.
        """
        patient = str(question.patient_id) if question.patient_id else None

        if capability in ("patient_lookup", "timeline_reconstruct"):
            return {"patient_id": patient} if patient else None
        if capability in ("diagnosis_lookup", "medication_lookup"):
            return {"patient_id": patient, "active_only": True} if patient else None
        if capability == "risk_score":
            return {"patient_id": patient, "score": "chads2_vasc"} if patient else None
        if capability == "lab_trend":
            analyte = next((a for a in _ANALYTES if a in text.lower()), None)
            if analyte is None or patient is None:
                return None
            return {"patient_id": patient, "analyte": analyte}
        if capability == "guideline_lookup":
            return {"topic": text}
        if capability == "graph_traversal":
            return {"concept": text, "max_hops": 2}
        if capability == "drug_interaction_check":
            # The question text goes to the tool, which resolves drug names against the
            # graph. The executor additionally merges in the patient's medication list when
            # a lookup ran, so both "do X and Y interact" and "do her medications interact"
            # reach the same check.
            return {"medications": [], "text": text}
        return None


def _confidence(top: float, runner_up: float, threshold: float) -> float:
    """Confidence from the margin between the best and second-best capability."""
    if top < threshold:
        return 0.0
    margin = (top - runner_up) / top if top else 0.0
    return round(min(1.0, 0.5 + 0.5 * margin), 4)


def _readable(capability: str) -> str:
    return capability.replace("_", " ")
