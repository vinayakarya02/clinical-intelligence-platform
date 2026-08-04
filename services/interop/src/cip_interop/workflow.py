"""Cross-system clinical workflows.

Referrals, lab orders, imaging orders, and discharge coordination, modelled as FHIR ``Task``
state machines over a ``ServiceRequest``. The shape comes from IHE 360X and the FHIR workflow
pattern, and the reason for using it rather than inventing one is the same as for care pathways
in Phase 5: an EHR that already speaks this can participate without a bespoke integration.

The property that matters is **closed loop**. A referral that is sent and never resolved is the
single most common workflow failure in healthcare, and it is invisible precisely because
nothing is in an error state — the task simply sits. So:

- every workflow has an explicit terminal set, and anything not terminal is *open*
- open tasks have an age, and age past a threshold is a reportable condition rather than a
  silent backlog
- a transition to a terminal state records who closed it and why

Transitions are table-driven. A state machine written as scattered ``if`` statements acquires an
undocumented path within two changes, and in a referral workflow an undocumented path means a
patient falls out of the loop.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_core.logging import get_logger
from cip_interop.domain import InteropError
from cip_interop.fhir.resources import Resource, fhir_id, make_reference
from cip_interop.orgs import AgreementRegistry, OrganizationContext
from cip_interop.streaming import ClinicalEventType, EventStream

__all__ = [
    "TaskState",
    "WorkflowError",
    "WorkflowKind",
    "WorkflowOrchestrator",
    "WorkflowTask",
]

_log = get_logger(__name__)


class WorkflowError(InteropError):
    """A workflow operation is not valid."""


class WorkflowKind(StrEnum):
    """The cross-system workflows this orchestrator runs."""

    REFERRAL = "referral"
    LAB_ORDER = "lab_order"
    IMAGING_ORDER = "imaging_order"
    DISCHARGE = "discharge"

    @property
    def crosses_organizations(self) -> bool:
        """Whether this workflow routinely leaves the initiating organisation.

        Those require a sharing agreement before the request is even sent — checking at
        fulfilment time means the patient has already been told a referral was made.
        """
        return self in (WorkflowKind.REFERRAL, WorkflowKind.LAB_ORDER, WorkflowKind.IMAGING_ORDER)

    @property
    def stale_after_hours(self) -> int:
        """When an open task of this kind becomes reportable.

        Different by kind because the clinical cost of a silent stall differs: an unfilled stat
        imaging order matters within hours, an outpatient referral within weeks.
        """
        return {
            "referral": 14 * 24,
            "lab_order": 48,
            "imaging_order": 72,
            "discharge": 24,
        }[self.value]


class TaskState(StrEnum):
    """FHIR ``Task.status``, restricted to the states this orchestrator uses."""

    DRAFT = "draft"
    REQUESTED = "requested"
    RECEIVED = "received"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    IN_PROGRESS = "in-progress"
    ON_HOLD = "on-hold"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (
            TaskState.COMPLETED,
            TaskState.CANCELLED,
            TaskState.REJECTED,
            TaskState.FAILED,
        )

    @property
    def closes_the_loop(self) -> bool:
        """Whether reaching this state means the requester learned the outcome.

        ``COMPLETED``, ``REJECTED``, and ``FAILED`` do. ``CANCELLED`` does too — the requester
        cancelled it. The distinction exists because "closed" and "closed successfully" are
        different measurements and conflating them makes a rejection rate of 40% look like a
        completion rate.
        """
        return self.is_terminal


#: The permitted transitions. A table rather than scattered conditionals, so the reachable
#: states are enumerable and a new path is a visible diff.
_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.DRAFT: frozenset({TaskState.REQUESTED, TaskState.CANCELLED}),
    TaskState.REQUESTED: frozenset(
        {TaskState.RECEIVED, TaskState.REJECTED, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.RECEIVED: frozenset(
        {TaskState.ACCEPTED, TaskState.REJECTED, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.ACCEPTED: frozenset(
        {TaskState.IN_PROGRESS, TaskState.ON_HOLD, TaskState.CANCELLED, TaskState.FAILED}
    ),
    TaskState.IN_PROGRESS: frozenset(
        {TaskState.COMPLETED, TaskState.ON_HOLD, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.ON_HOLD: frozenset({TaskState.IN_PROGRESS, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.REJECTED: frozenset(),
    TaskState.FAILED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class TaskTransition:
    """One recorded state change."""

    from_state: TaskState
    to_state: TaskState
    at: dt.datetime
    by: str
    reason: str = ""

    def render(self) -> str:
        detail = f" ({self.reason})" if self.reason else ""
        return f"{self.from_state.value} -> {self.to_state.value} by {self.by}{detail}"


@dataclass(frozen=True, slots=True)
class WorkflowTask:
    """One unit of cross-system work."""

    task_id: str
    kind: WorkflowKind
    state: TaskState
    person_id: str
    requesting_organization_id: str
    performing_organization_id: str
    focus_reference: str = ""
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    updated_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    history: tuple[TaskTransition, ...] = ()
    priority: str = "routine"
    note: str = ""

    @property
    def open(self) -> bool:
        return not self.state.is_terminal

    def age_hours(self, now: dt.datetime | None = None) -> float:
        return ((now or dt.datetime.now(dt.UTC)) - self.created_at).total_seconds() / 3600

    def is_stale(self, now: dt.datetime | None = None) -> bool:
        """Whether this open task has waited longer than its kind allows.

        The measurement that makes a closed loop real. A referral nobody rejected and nobody
        completed is invisible without it, because nothing is in an error state.
        """
        return self.open and self.age_hours(now) > self.kind.stale_after_hours

    def to_fhir(self) -> Resource:
        data: dict[str, Any] = {
            "resourceType": "Task",
            "id": fhir_id(self.task_id),
            "status": self.state.value,
            "intent": "order",
            "for": make_reference("Patient", self.person_id),
            "authoredOn": self.created_at.isoformat(),
            "lastModified": self.updated_at.isoformat(),
            "identifier": [{"system": "urn:cip:workflow", "value": self.task_id}],
        }
        if self.focus_reference and "/" in self.focus_reference:
            resource_type, identifier = self.focus_reference.split("/", 1)
            data["focus"] = make_reference(resource_type, identifier)
        if self.performing_organization_id:
            data["owner"] = make_reference("Organization", self.performing_organization_id)
        if self.note:
            data["statusReason"] = {"text": self.note}
        return Resource(
            resource_type="Task", data=data, organization_id=self.requesting_organization_id
        )


class WorkflowOrchestrator:
    """Runs cross-system workflows.

    Holds the sharing-agreement registry because a cross-organisation workflow is refused at
    *initiation* if no agreement covers it. Checking at fulfilment would mean the patient has
    already been told a referral was made to somewhere that cannot legally receive it.
    """

    def __init__(
        self,
        *,
        agreements: AgreementRegistry,
        stream: EventStream | None = None,
        max_tasks: int = 100_000,
    ) -> None:
        self._agreements = agreements
        self._stream = stream
        self._tasks: dict[str, WorkflowTask] = {}
        self._max_tasks = max_tasks

    def initiate(
        self,
        kind: WorkflowKind,
        *,
        person_id: str,
        context: OrganizationContext,
        performing_organization_id: str,
        purpose: Any = None,
        focus_reference: str = "",
        priority: str = "routine",
        at: dt.datetime | None = None,
    ) -> WorkflowTask:
        """Start a workflow.

        A cross-organisation workflow with no covering agreement is refused here, with the
        reason naming what is missing.
        """
        from cip_interop.domain import PurposeOfUse

        moment = at or dt.datetime.now(dt.UTC)
        effective_purpose = purpose or PurposeOfUse.TREATMENT

        if kind.crosses_organizations and performing_organization_id != context.organization_id:
            decision = self._agreements.evaluate(
                source_organization_id=context.organization_id,
                target_organization_id=performing_organization_id,
                purpose=effective_purpose,
                on=moment.date(),
            )
            if not decision.permitted:
                raise WorkflowError(
                    f"cannot initiate a {kind.value} from {context.organization_id} to "
                    f"{performing_organization_id}: {decision.reason}"
                )

        task = WorkflowTask(
            task_id=f"task:{uuid.uuid4()}",
            kind=kind,
            state=TaskState.REQUESTED,
            person_id=person_id,
            requesting_organization_id=context.organization_id,
            performing_organization_id=performing_organization_id,
            focus_reference=focus_reference,
            created_at=moment,
            updated_at=moment,
            priority=priority,
            history=(
                TaskTransition(
                    from_state=TaskState.DRAFT,
                    to_state=TaskState.REQUESTED,
                    at=moment,
                    by=context.principal_id,
                    reason="initiated",
                ),
            ),
        )
        self._tasks[task.task_id] = task
        self._evict_closed()

        if self._stream and kind is WorkflowKind.REFERRAL:
            self._stream.publish(
                ClinicalEventType.REFERRAL_INITIATED,
                partition_key=person_id,
                payload={"task_id": task.task_id, "to": performing_organization_id},
                organization_id=context.organization_id,
                occurred_at=moment,
            )
        _log.info(
            "workflow.initiated",
            kind=kind.value,
            task=task.task_id,
            performer=performing_organization_id,
        )
        return task

    def transition(
        self,
        task_id: str,
        to_state: TaskState,
        *,
        by: str,
        reason: str = "",
        at: dt.datetime | None = None,
    ) -> WorkflowTask:
        """Move a task, refusing any transition the table does not permit."""
        task = self._tasks.get(task_id)
        if task is None:
            raise WorkflowError(f"unknown task {task_id!r}")
        if not by.strip():
            raise WorkflowError(
                "a transition requires a named actor; an anonymous state change cannot be "
                "questioned afterwards"
            )
        permitted = _TRANSITIONS[task.state]
        if to_state not in permitted:
            raise WorkflowError(
                f"{task.state.value} -> {to_state.value} is not a permitted transition. "
                f"From {task.state.value} the reachable states are: "
                + (", ".join(sorted(s.value for s in permitted)) or "none (terminal)")
            )
        if to_state.is_terminal and not reason.strip():
            raise WorkflowError(
                f"closing a task as {to_state.value} requires a reason; a loop closed without "
                "one cannot be distinguished from one abandoned"
            )

        moment = at or dt.datetime.now(dt.UTC)
        updated = WorkflowTask(
            task_id=task.task_id,
            kind=task.kind,
            state=to_state,
            person_id=task.person_id,
            requesting_organization_id=task.requesting_organization_id,
            performing_organization_id=task.performing_organization_id,
            focus_reference=task.focus_reference,
            created_at=task.created_at,
            updated_at=moment,
            history=(
                *task.history,
                TaskTransition(
                    from_state=task.state, to_state=to_state, at=moment, by=by, reason=reason
                ),
            ),
            priority=task.priority,
            note=reason or task.note,
        )
        self._tasks[task_id] = updated

        if self._stream and to_state.is_terminal:
            self._stream.publish(
                ClinicalEventType.WORKFLOW_COMPLETED,
                partition_key=task.person_id,
                payload={
                    "task_id": task_id,
                    "kind": task.kind.value,
                    "final_state": to_state.value,
                },
                organization_id=task.requesting_organization_id,
                occurred_at=moment,
            )
        return updated

    def _evict_closed(self) -> None:
        """Bound the task map, never evicting an open task.

        An open task is work somebody is waiting on. Dropping one to save memory loses a
        referral, which is the exact failure this whole module exists to prevent.
        """
        if len(self._tasks) <= self._max_tasks:
            return
        closed = sorted((t for t in self._tasks.values() if not t.open), key=lambda t: t.updated_at)
        excess = len(self._tasks) - self._max_tasks
        for task in closed[:excess]:
            del self._tasks[task.task_id]

    def task(self, task_id: str) -> WorkflowTask:
        found = self._tasks.get(task_id)
        if found is None:
            raise WorkflowError(f"unknown task {task_id!r}")
        return found

    def open_tasks(self, *, kind: WorkflowKind | None = None) -> tuple[WorkflowTask, ...]:
        return tuple(t for t in self._tasks.values() if t.open and (kind is None or t.kind is kind))

    def stale_tasks(self, *, now: dt.datetime | None = None) -> tuple[WorkflowTask, ...]:
        """Open tasks past their kind's threshold. The closed-loop report."""
        return tuple(t for t in self._tasks.values() if t.is_stale(now))

    def closure_rate(self, kind: WorkflowKind) -> float | None:
        """Fraction of this kind's tasks that reached a terminal state.

        ``None`` when there are none — not zero, which would read as total failure.
        """
        relevant = [t for t in self._tasks.values() if t.kind is kind]
        if not relevant:
            return None
        closed = sum(1 for t in relevant if not t.open)
        return round(closed / len(relevant), 4)

    def statistics(self, *, now: dt.datetime | None = None) -> dict[str, Any]:
        return {
            "tasks": len(self._tasks),
            "open": len(self.open_tasks()),
            "stale": len(self.stale_tasks(now=now)),
            "closure_rate": {
                kind.value: self.closure_rate(kind)
                for kind in WorkflowKind
                if self.closure_rate(kind) is not None
            },
        }
