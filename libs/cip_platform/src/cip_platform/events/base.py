"""Event spine: envelopes, the bus contract, and the catalogue.

Every event carries a tenant, a correlation id, a causation id, and W3C trace context, so a
document's whole lifecycle reconstructs as one trace across processes
(docs/design/adr-0015-event-spine.md).

The decision worth reading is that **the bus emits ``AuditLogged`` itself**. HIPAA
§164.312(b) requires a record of access to PHI, and the usual implementation is a call to an
audit function at each point that matters — a requirement satisfied by every developer
remembering. Emitting it from the bus makes "was this audited" a property of the bus.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "Event",
    "EventBus",
    "EventHandler",
    "EventType",
]


class EventType(StrEnum):
    """The document and answer lifecycle.

    A closed set, not free-form strings: a consumer subscribing to a typo'd event name
    subscribes to silence, and nothing about that failure is visible until someone asks why a
    downstream index is empty.
    """

    DOCUMENT_UPLOADED = "DocumentUploaded"
    DOCUMENT_PARSED = "DocumentParsed"
    CHUNK_CREATED = "ChunkCreated"
    EMBEDDING_GENERATED = "EmbeddingGenerated"
    GRAPH_UPDATED = "GraphUpdated"
    EVALUATION_COMPLETED = "EvaluationCompleted"
    ANSWER_PRODUCED = "AnswerProduced"
    AUDIT_LOGGED = "AuditLogged"

    @property
    def carries_phi(self) -> bool:
        """Whether a payload of this type may contain protected health information.

        Drives what the bus is willing to log and what it will export to telemetry. Marked on
        the type rather than checked per publish, so a new event type has to make the decision
        explicitly.
        """
        return self in (
            EventType.DOCUMENT_UPLOADED,
            EventType.DOCUMENT_PARSED,
            EventType.CHUNK_CREATED,
            EventType.ANSWER_PRODUCED,
        )


@dataclass(frozen=True, slots=True)
class Event:
    """One published fact.

    Immutable and self-describing. ``causation_id`` is the id of the event that caused this
    one and ``correlation_id`` is constant across a whole lifecycle, which is the pair that
    lets a chain be reconstructed *and* attributed — one says what came before, the other says
    which run it belongs to.
    """

    type: EventType
    tenant_id: uuid.UUID
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: uuid.UUID = field(default_factory=uuid.uuid4)
    correlation_id: str = ""
    causation_id: uuid.UUID | None = None
    traceparent: str = ""
    """W3C trace context. Carried on the envelope so an asynchronous consumer joins the
    producer's trace rather than starting an orphan one."""
    occurred_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    source: str = "unknown"

    def caused(self, event_type: EventType, **payload: Any) -> Event:
        """A follow-on event, inheriting correlation and trace context.

        The ergonomic reason this exists: a handler that has to remember to copy four fields
        will eventually copy three, and the resulting event is orphaned from its trace with no
        error anywhere.
        """
        return Event(
            type=event_type,
            tenant_id=self.tenant_id,
            payload=payload,
            correlation_id=self.correlation_id,
            causation_id=self.event_id,
            traceparent=self.traceparent,
            source=self.source,
        )

    def partition_key(self) -> str:
        """Ordering key. Per-tenant, which is the only ordering any consumer here needs."""
        return str(self.tenant_id)

    def to_json(self) -> dict[str, Any]:
        return {
            "event_id": str(self.event_id),
            "type": str(self.type),
            "tenant_id": str(self.tenant_id),
            "correlation_id": self.correlation_id,
            "causation_id": str(self.causation_id) if self.causation_id else None,
            "traceparent": self.traceparent,
            "occurred_at": self.occurred_at.isoformat(),
            "source": self.source,
            "payload": self.payload,
        }

    def audit_summary(self) -> dict[str, Any]:
        """What may be written to the audit log.

        PHI-carrying payloads are summarised to their keys, never their values. The audit log
        is queried by operators and retained for years; putting clinical content in it turns a
        compliance control into a second, less-guarded copy of the record.
        """
        return {
            "event_id": str(self.event_id),
            "type": str(self.type),
            "tenant_id": str(self.tenant_id),
            "correlation_id": self.correlation_id,
            "occurred_at": self.occurred_at.isoformat(),
            "payload_keys": sorted(self.payload) if self.type.carries_phi else self.payload,
        }


@runtime_checkable
class EventHandler(Protocol):
    """Consumes events of one type."""

    async def handle(self, event: Event) -> None: ...


@runtime_checkable
class EventBus(Protocol):
    """Publishes events and dispatches them to subscribers."""

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None: ...

    async def publish(self, event: Event) -> None: ...

    async def publish_many(self, events: list[Event]) -> None: ...
