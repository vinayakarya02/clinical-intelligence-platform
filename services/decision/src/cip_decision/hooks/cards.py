"""CDS Hooks cards and services.

Implements the CDS Hooks 2.0 response shape faithfully: discovery, service invocation, and
Cards with ``summary``, ``indicator``, ``source``, ``suggestions``, ``selectionBehavior``,
``overrideReasons``, and ``links``.

Conforming to the specification rather than inventing a card format is the point — a CDS
client that has never heard of this platform can render its output.

``medication-prescribe`` is implemented **and marked deprecated**, because the specification
deprecated it in favour of ``order-select`` and ``order-sign``. Shipping it silently would
encode a stale integration into every deployment that copied the example.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_core.logging import get_logger
from cip_decision.domain import Recommendation, Severity

__all__ = [
    "Card",
    "HookType",
    "Source",
    "Suggestion",
    "SuggestionAction",
    "build_card",
    "discovery_document",
]

_log = get_logger(__name__)

#: The specification's limit. Enforced by ``Recommendation`` at construction so a
#: recommendation that cannot be rendered as a card cannot be created.
_SUMMARY_LIMIT = 140


class HookType(StrEnum):
    """Hooks this platform serves."""

    PATIENT_VIEW = "patient-view"
    ORDER_SELECT = "order-select"
    ORDER_SIGN = "order-sign"
    ENCOUNTER_DISCHARGE = "encounter-discharge"
    MEDICATION_PRESCRIBE = "medication-prescribe"

    @property
    def is_deprecated(self) -> bool:
        """Whether the specification has deprecated this hook.

        Reported in discovery so an integrator sees it before building against it, rather
        than after.
        """
        return self is HookType.MEDICATION_PRESCRIBE

    @property
    def replacement(self) -> str:
        return "order-select, order-sign" if self.is_deprecated else ""


@dataclass(frozen=True, slots=True)
class Source:
    """Where a card's advice came from. ``label`` is required by the specification."""

    label: str
    url: str = ""
    icon: str = ""
    topic: dict[str, str] | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"label": self.label}
        if self.url:
            payload["url"] = self.url
        if self.icon:
            payload["icon"] = self.icon
        if self.topic:
            payload["topic"] = self.topic
        return payload


@dataclass(frozen=True, slots=True)
class SuggestionAction:
    """A proposed FHIR operation."""

    type: str
    description: str
    resource: dict[str, Any] | None = None
    resource_id: str = ""

    def __post_init__(self) -> None:
        if self.type not in ("create", "update", "delete"):
            raise ValueError(f"Action type must be create, update, or delete; got '{self.type}'")
        if self.type in ("create", "update") and self.resource is None:
            raise ValueError(f"A {self.type} action requires a resource")
        if self.type == "delete" and not self.resource_id:
            raise ValueError("A delete action requires a resourceId")

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.resource is not None:
            payload["resource"] = self.resource
        if self.resource_id:
            payload["resourceId"] = self.resource_id
        return payload


@dataclass(frozen=True, slots=True)
class Suggestion:
    """An actionable proposal on a card."""

    label: str
    uuid: str = field(default_factory=lambda: str(uuid.uuid4()))
    is_recommended: bool = False
    actions: tuple[SuggestionAction, ...] = ()

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"label": self.label, "uuid": self.uuid}
        if self.is_recommended:
            payload["isRecommended"] = True
        if self.actions:
            payload["actions"] = [a.to_json() for a in self.actions]
        return payload


@dataclass(frozen=True, slots=True)
class Card:
    """One CDS Hooks card."""

    summary: str
    indicator: str
    source: Source
    uuid: str = field(default_factory=lambda: str(uuid.uuid4()))
    detail: str = ""
    suggestions: tuple[Suggestion, ...] = ()
    selection_behavior: str = ""
    override_reasons: tuple[dict[str, str], ...] = ()
    links: tuple[dict[str, str], ...] = ()

    def __post_init__(self) -> None:
        if len(self.summary) > _SUMMARY_LIMIT:
            raise ValueError(
                f"Card summary must be under {_SUMMARY_LIMIT} characters (got {len(self.summary)})"
            )
        if self.indicator not in ("info", "warning", "critical"):
            raise ValueError(
                f"Card indicator must be info, warning, or critical; got '{self.indicator}'"
            )
        if self.suggestions and not self.selection_behavior:
            # The specification requires it whenever suggestions are present; a client
            # otherwise cannot know whether the suggestions are mutually exclusive.
            raise ValueError("A card with suggestions must set selectionBehavior")
        if self.selection_behavior and self.selection_behavior not in ("at-most-one", "any"):
            raise ValueError(
                f"selectionBehavior must be at-most-one or any; got '{self.selection_behavior}'"
            )

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "uuid": self.uuid,
            "summary": self.summary,
            "indicator": self.indicator,
            "source": self.source.to_json(),
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.suggestions:
            payload["suggestions"] = [s.to_json() for s in self.suggestions]
            payload["selectionBehavior"] = self.selection_behavior
        if self.override_reasons:
            payload["overrideReasons"] = list(self.override_reasons)
        if self.links:
            payload["links"] = list(self.links)
        return payload


#: Offered whenever a card can be dismissed. Structured rather than free text because an
#: override reason is the system's only direct measurement of whether a rule is right, and
#: free text cannot be counted (docs/design/adr-0021-alert-fatigue.md).
_STANDARD_OVERRIDE_REASONS: tuple[dict[str, str], ...] = (
    {"code": "clinically-inappropriate", "display": "Not clinically appropriate here"},
    {"code": "already-addressed", "display": "Already addressed"},
    {"code": "patient-preference", "display": "Patient preference"},
    {"code": "benefit-outweighs-risk", "display": "Benefit outweighs the risk"},
    {"code": "will-monitor", "display": "Will monitor instead"},
    {"code": "data-incorrect", "display": "The underlying data is incorrect"},
)


def build_card(recommendation: Recommendation, *, base_url: str = "") -> Card:
    """Render a recommendation as a conformant card.

    The ``detail`` carries the full explanation — evidence quality, rationale, citations, and
    derivation — because a card a clinician cannot check is a card they can only obey or
    dismiss, and dismissal is what the alert-fatigue literature measures.
    """
    citation = recommendation.citations[0]
    source = Source(
        label=citation.source,
        url=citation.url or base_url,
        topic=(
            {"code": recommendation.kind.value, "display": recommendation.kind.value}
            if recommendation.kind
            else None
        ),
    )

    suggestions: tuple[Suggestion, ...] = ()
    selection_behavior = ""
    if recommendation.kind.value in ("investigation", "monitoring"):
        # A suggestion carries a concrete action a client can act on. Only offered where the
        # action is unambiguous — proposing a medication change as a one-click action would
        # be exactly the automation this platform refuses (ADR-0024).
        suggestions = (
            Suggestion(
                label=recommendation.summary,
                is_recommended=recommendation.severity.rank >= Severity.MAJOR.rank,
            ),
        )
        selection_behavior = "at-most-one"

    return Card(
        summary=recommendation.summary,
        indicator=recommendation.severity.cds_hooks_indicator,
        source=source,
        detail=recommendation.explain(),
        suggestions=suggestions,
        selection_behavior=selection_behavior,
        override_reasons=_STANDARD_OVERRIDE_REASONS,
    )


def discovery_document(services: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    """The ``/cds-services`` discovery response."""
    return {"services": list(services)}


def service_definition(
    *,
    hook: HookType,
    service_id: str,
    title: str,
    description: str,
    prefetch: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One entry in the discovery document.

    A deprecated hook says so in its description, so an integrator reading discovery sees it
    before building against it.
    """
    described = description
    if hook.is_deprecated:
        described = (
            f"{description} DEPRECATED by the CDS Hooks specification in favour of "
            f"{hook.replacement}; provided for compatibility with existing clients."
        )
    payload: dict[str, Any] = {
        "hook": hook.value,
        "id": service_id,
        "title": title,
        "description": described,
    }
    if prefetch:
        payload["prefetch"] = prefetch
    return payload
