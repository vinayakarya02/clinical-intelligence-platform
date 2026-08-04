"""In-process event bus.

Dispatches synchronously, which is deliberate for tests: a failure surfaces at the ``publish``
call rather than asynchronously somewhere else, and an assertion after a publish sees the
handlers' effects. Production behaviour differs — that difference is real, and integration
tests are the only place it is visible (docs/design/adr-0015-event-spine.md).

Two properties are shared with the Kafka backend, so a handler behaves the same against
either:

**A failing handler does not stop the others.** One broken consumer must not prevent the graph
from being updated, so failures are isolated and recorded per handler.

**Every published event produces an ``AuditLogged``.** Emitted by the bus, not by handlers.
"""

from __future__ import annotations

from collections import defaultdict, deque

from cip_core.logging import get_logger
from cip_platform.events.base import Event, EventHandler, EventType

__all__ = ["InMemoryEventBus"]

_log = get_logger(__name__)


class InMemoryEventBus:
    """Synchronous in-process dispatch."""

    def __init__(
        self,
        *,
        audit_sink: EventHandler | None = None,
        history_limit: int = 1000,
    ) -> None:
        self._handlers: dict[EventType, list[EventHandler]] = defaultdict(list)
        self._audit_sink = audit_sink
        # Bounded ring, not an unbounded list. This bus is the *development* backend as well
        # as the test one, so it runs in a long-lived process: an unbounded history grows one
        # entry per event forever, and because those entries hold full payloads it retains
        # clinical content indefinitely — a memory leak and a PHI-retention problem at once.
        self._published: deque[Event] = deque(maxlen=history_limit)
        self._failures: deque[tuple[str, str]] = deque(maxlen=history_limit)

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event: Event) -> None:
        """Dispatch to every subscriber, then audit."""
        self._published.append(event)

        for handler in list(self._handlers.get(event.type, ())):
            try:
                await handler.handle(event)
            except Exception as exc:
                # Isolated: one broken consumer must not stop the graph from being updated.
                # Recorded rather than swallowed, so a consistently failing handler is
                # visible instead of merely quiet.
                self._failures.append((type(handler).__name__, type(exc).__name__))
                _log.warning(
                    "events.handler_failed",
                    event_type=str(event.type),
                    handler=type(handler).__name__,
                    error=type(exc).__name__,
                )

        if event.type is not EventType.AUDIT_LOGGED:
            await self._audit(event)

    async def publish_many(self, events: list[Event]) -> None:
        for event in events:
            await self.publish(event)

    async def _audit(self, event: Event) -> None:
        """Emit the audit record for ``event``.

        Guarded against recursion by the caller's check on ``AUDIT_LOGGED``: auditing an audit
        event would recurse forever, and doing it here keeps that reasoning next to the check.
        """
        record = Event(
            type=EventType.AUDIT_LOGGED,
            tenant_id=event.tenant_id,
            payload=event.audit_summary(),
            correlation_id=event.correlation_id,
            causation_id=event.event_id,
            traceparent=event.traceparent,
            source=event.source,
        )
        self._published.append(record)
        for handler in list(self._handlers.get(EventType.AUDIT_LOGGED, ())):
            try:
                await handler.handle(record)
            except Exception as exc:
                # An audit sink failing is more serious than an ordinary handler failing, so
                # it is logged at error. It still must not fail the request: refusing clinical
                # work because a log sink is down trades a compliance gap for an outage.
                self._failures.append((type(handler).__name__, type(exc).__name__))
                _log.error(
                    "events.audit_handler_failed",
                    handler=type(handler).__name__,
                    error=type(exc).__name__,
                )
        if self._audit_sink is not None:
            try:
                await self._audit_sink.handle(record)
            except Exception as exc:
                self._failures.append(("audit_sink", type(exc).__name__))
                _log.error("events.audit_sink_failed", error=type(exc).__name__)

    def published(self, event_type: EventType | None = None) -> list[Event]:
        """Everything published so far. Test helper; not part of the protocol."""
        if event_type is None:
            return list(self._published)
        return [e for e in self._published if e.type is event_type]

    def failures(self) -> list[tuple[str, str]]:
        return list(self._failures)

    def clear(self) -> None:
        self._published.clear()
        self._failures.clear()
