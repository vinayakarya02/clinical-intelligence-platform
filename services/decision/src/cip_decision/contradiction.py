"""Contradiction and missing-information detection.

Two checks that run after recommendations are assembled and before they are shown.

**Contradiction** finds recommendations that cannot both be acted on. The engine never
silently resolves one — it surfaces the pair together and says they conflict, because a system
that quietly picks a side has made a clinical decision without a clinician and without saying
so.

**Missing information** turns unevaluable rules into a statement of what would have been
needed. That is the difference between "no concerns" and "three rules could not be evaluated
because no potassium is recorded", and the second is what a clinician needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from cip_core.logging import get_logger
from cip_decision.domain import Recommendation, RecommendationKind
from cip_decision.rules.engine import RuleTrace

__all__ = [
    "Contradiction",
    "ContradictionReport",
    "Direction",
    "detect_contradictions",
    "missing_information",
]

_log = get_logger(__name__)

#: Recommendation kinds that can conflict. Two monitoring suggestions never contradict each
#: other; two medication changes to the same drug can.
_CONFLICTABLE = frozenset({RecommendationKind.MEDICATION_CHANGE, RecommendationKind.ALERT})


class Direction(StrEnum):
    """Which way a recommendation points about its subject.

    Declared by the knowledge author on the recommendation's metadata. There is no inference
    from prose — see :func:`detect_contradictions` for why two attempts at that were removed.
    """

    TOWARD = "toward"
    """Recommends starting, adding, or continuing the agent."""

    AWAY = "away"
    """Recommends stopping, withholding, or avoiding it."""

    UNSTATED = "unstated"
    """No direction was declared. Participates in no contradiction."""


@dataclass(frozen=True, slots=True)
class Contradiction:
    """Two recommendations that cannot both be acted on."""

    left: Recommendation
    right: Recommendation
    subject: str
    explanation: str

    def render(self) -> str:
        return (
            f"Conflict on {self.subject}: '{self.left.summary}' versus "
            f"'{self.right.summary}' — {self.explanation}"
        )


@dataclass(frozen=True, slots=True)
class ContradictionReport:
    """Every conflict found."""

    contradictions: tuple[Contradiction, ...] = ()

    @property
    def has_conflicts(self) -> bool:
        return bool(self.contradictions)

    def subjects(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(c.subject for c in self.contradictions))


def detect_contradictions(
    recommendations: tuple[Recommendation, ...],
) -> ContradictionReport:
    """Find recommendations that cannot both be acted on.

    Direction is **declared, never inferred from prose.** Two earlier attempts inferred it
    from wording and both produced false conflicts — the second flagged "2 statin agents
    prescribed concurrently" as recommending *toward* a statin, because the word "prescribed"
    appears in a sentence describing the current state rather than proposing an action.

    False conflicts cost exactly the clinician attention that the alert-fatigue research
    identifies as the scarce resource, so a recommendation whose knowledge author did not
    state a direction contributes to no contradiction at all. Missing a contradiction is
    recoverable by the reviewing clinician the approval gate guarantees; fabricating one is
    not recoverable, because it spends attention and teaches the clinician to distrust the
    detector.
    """
    found: list[Contradiction] = []
    conflictable = [r for r in recommendations if r.kind in _CONFLICTABLE]

    for index, left in enumerate(conflictable):
        for right in conflictable[index + 1 :]:
            shared = set(left.triggering_facts) & set(right.triggering_facts)
            if not shared:
                continue

            left_direction = _declared_direction(left)
            right_direction = _declared_direction(right)
            if {left_direction, right_direction} != {Direction.TOWARD, Direction.AWAY}:
                continue

            subject = sorted(shared)[0]
            found.append(
                Contradiction(
                    left=left,
                    right=right,
                    subject=subject,
                    explanation=(
                        "one recommends starting or continuing this agent while the other "
                        "recommends stopping or avoiding it"
                    ),
                )
            )

    if found:
        _log.warning("contradiction.detected", count=len(found))
    return ContradictionReport(contradictions=tuple(found))


def _declared_direction(recommendation: Recommendation) -> Direction:
    """The direction the knowledge author declared, or ``UNSTATED``.

    ``UNSTATED`` participates in no contradiction. That is the deliberate cost of refusing to
    guess: a knowledge base that wants contradiction detection has to say which way each
    recommendation points, which is a small authoring burden and the only way the detector
    can be trusted.
    """
    raw = str(recommendation.metadata.get("direction", "")).lower()
    try:
        return Direction(raw)
    except ValueError:
        return Direction.UNSTATED


def missing_information(trace: RuleTrace) -> tuple[str, ...]:
    """What, if known, would let an unevaluable rule be evaluated.

    The output that distinguishes "no concerns" from "we could not check". A clinician told
    the system found nothing, when in fact three rules could not run, has been misled by
    silence.
    """
    return trace.missing_information()
