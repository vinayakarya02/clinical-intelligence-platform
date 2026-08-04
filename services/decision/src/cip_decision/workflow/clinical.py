"""Event-driven clinical workflow.

Wires the decision engine to clinical events:

``lab result arrives → rules → drug checks → risk recalculation → recommendations → notify``

Built on the Phase 4 event spine rather than a new mechanism, so a clinical workflow gets the
same correlation, causation, tracing, and automatic audit every other event does. The workflow
declares *what* happens on an event; the bus decides how it is delivered.

The notification step is a protocol with no default implementation. A workflow that silently
does nothing when nobody is listening is preferable to one that invents a delivery channel —
and a deployment that forgot to wire notification should discover it in configuration, not by
a clinician never being told.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from cip_core.logging import get_logger
from cip_decision.domain import PatientContext, Recommendation, Severity
from cip_decision.engine import DecisionEngine, DecisionResult

__all__ = [
    "ClinicalEvent",
    "ClinicalEventType",
    "ClinicalWorkflow",
    "Notifier",
    "WorkflowRun",
]

_log = get_logger(__name__)


class ClinicalEventType(StrEnum):
    """Clinical events that trigger a decision run."""

    LAB_RESULT_AVAILABLE = "lab_result_available"
    MEDICATION_PRESCRIBED = "medication_prescribed"
    PATIENT_ADMITTED = "patient_admitted"
    PATIENT_DISCHARGED = "patient_discharged"
    DIAGNOSIS_RECORDED = "diagnosis_recorded"
    PERIODIC_REVIEW = "periodic_review"

    @property
    def default_urgency(self) -> Severity:
        """The severity floor a notification for this event must clear.

        An admission review is routine and should not page anybody; a prescription check is
        the moment an interaction matters most and warrants a lower floor.
        """
        return {
            "lab_result_available": Severity.MAJOR,
            "medication_prescribed": Severity.MODERATE,
            "patient_admitted": Severity.MAJOR,
            "patient_discharged": Severity.MAJOR,
            "diagnosis_recorded": Severity.MAJOR,
            "periodic_review": Severity.MAJOR,
        }[self.value]


@dataclass(frozen=True, slots=True)
class ClinicalEvent:
    """Something happened that may change the clinical picture."""

    type: ClinicalEventType
    patient_id: uuid.UUID
    tenant_id: uuid.UUID
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    occurred_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))


@runtime_checkable
class Notifier(Protocol):
    """Delivers recommendations to a clinician."""

    async def notify(
        self, *, event: ClinicalEvent, recommendations: tuple[Recommendation, ...]
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """One event, processed."""

    event: ClinicalEvent
    result: DecisionResult
    notified: tuple[Recommendation, ...] = ()
    withheld: tuple[Recommendation, ...] = ()
    duration_ms: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "event_type": str(self.event.type),
            "patient_id": str(self.event.patient_id),
            "recommendations": len(self.result.recommendations),
            "notified": len(self.notified),
            "withheld_below_urgency": len(self.withheld),
            "duration_ms": round(self.duration_ms, 3),
        }


class ClinicalWorkflow:
    """Runs the decision engine in response to clinical events."""

    def __init__(
        self,
        engine: DecisionEngine,
        *,
        notifier: Notifier | None = None,
        context_provider: Any = None,
    ) -> None:
        self._engine = engine
        self._notifier = notifier
        self._context_provider = context_provider
        """Resolves an event to a patient context. Injected so the workflow does no I/O of its
        own, which is what keeps it testable with a hand-built context."""

    async def handle(
        self, event: ClinicalEvent, *, context: PatientContext | None = None
    ) -> WorkflowRun:
        """Process one clinical event end to end."""
        import time

        started = time.perf_counter()

        resolved = context
        if resolved is None:
            if self._context_provider is None:
                raise ValueError("A clinical workflow needs either a context or a context provider")
            resolved = await self._context_provider(event)

        result = self._engine.decide(resolved)

        # Notification has its own severity floor, separate from the suppression floor. A
        # recommendation can be worth showing on a screen the clinician is already looking at
        # and not worth interrupting them for, and collapsing the two would either page
        # constantly or bury the urgent case.
        floor = event.type.default_urgency
        notify = tuple(r for r in result.recommendations if r.severity.rank >= floor.rank)
        withheld = tuple(r for r in result.recommendations if r.severity.rank < floor.rank)

        if notify and self._notifier is not None:
            await self._notifier.notify(event=event, recommendations=notify)
        elif notify:
            _log.warning(
                "workflow.no_notifier",
                event_type=str(event.type),
                undelivered=len(notify),
                detail="recommendations were produced but no notifier is configured",
            )

        run = WorkflowRun(
            event=event,
            result=result,
            notified=notify,
            withheld=withheld,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        _log.info("workflow.handled", **run.to_json())
        return run
