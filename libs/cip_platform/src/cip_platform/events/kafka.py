"""The Kafka event backbone.

``PlatformSettings`` has validated ``events_backend in ("memory", "kafka")`` since Phase 4 and
refused ``memory`` in a deployed environment. Nothing implemented Kafka, so production named a
backbone that could not be built. This is it.

**Partitioning is by tenant, and that is the ordering contract.** ``Event.partition_key()``
returns the tenant id, so every event for one tenant lands on one partition and is consumed in
publication order. Ordering *across* tenants is not guaranteed and is not needed — no consumer
here reasons about two tenants at once, and requiring global ordering would mean one partition
and no horizontal consumption at all.

**Publishing waits for the broker's acknowledgement.** ``acks="all"`` with an idempotent
producer: a publish that returns before the write is replicated is a publish that can vanish
during a broker failover, and the caller has already told the user their document was accepted.
The cost is latency on the publish path, which is the correct trade for clinical events.

This class implements the ``EventBus`` protocol. ``subscribe`` registers local handlers for the
in-process consumer loop; a separate consumer group in another process is the production
topology and is W1 work.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from cip_core.logging import get_logger
from cip_platform.events.base import Event, EventHandler, EventType

__all__ = ["KafkaEventBus", "topic_for"]

_log = get_logger(__name__)

#: One topic per event type, prefixed. Per-type rather than one firehose because retention,
#: partition count, and access control are all per-type decisions — a PHI-carrying topic and an
#: operational-metric topic should not share a retention policy.
_TOPIC_PREFIX = "cip"


def topic_for(event_type: EventType) -> str:
    """The topic an event type publishes to.

    Dots become hyphens: Kafka permits dots in topic names but its own metric names use them as
    separators, so a dotted topic produces metrics that cannot be parsed unambiguously.
    """
    return f"{_TOPIC_PREFIX}.{str(event_type).replace('.', '-')}"


class KafkaEventBus:
    """An ``EventBus`` over Kafka.

    Construction does not connect — :meth:`connect` does. That split is what lets the composition
    root build the whole platform synchronously and open sockets afterwards, and what lets a unit
    test construct this object without a broker.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        *,
        client_id: str = "cip-platform",
        acks: str = "all",
        request_timeout_ms: int = 10_000,
    ) -> None:
        if not bootstrap_servers.strip():
            raise ValueError("bootstrap_servers is required for the kafka event backbone")
        self._bootstrap = bootstrap_servers
        self._client_id = client_id
        self._acks = acks
        self._request_timeout_ms = request_timeout_ms
        self._producer: Any = None
        self._handlers: dict[EventType, list[EventHandler]] = {}

    # -- lifecycle ------------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the producer.

        ``enable_idempotence`` makes a retried publish safe: without it, a produce that succeeds
        but whose acknowledgement is lost is retried and duplicated, and a duplicate clinical
        event is a duplicate downstream action.

        A failed ``start()`` leaves nothing behind. Assigning ``self._producer`` before starting
        it — as this did until W6 — makes a broker that is briefly unavailable at boot
        permanent: ``is_connected`` reports true for a producer that never connected, and
        because ``connect`` returns early when ``_producer`` is set, every retry is a no-op. The
        process then publishes nothing for the rest of its life while reporting itself connected,
        and the outbox relay marks rows published against a cluster it never reached. Found by
        ``test_publishing_to_an_unreachable_broker_fails_rather_than_silently_dropping``, which
        is the first test to have ever pointed this class at a closed port.
        """
        if self._producer is not None:
            return
        from aiokafka import AIOKafkaProducer

        producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap,
            client_id=self._client_id,
            acks=self._acks,
            enable_idempotence=True,
            request_timeout_ms=self._request_timeout_ms,
            value_serializer=lambda value: json.dumps(value, sort_keys=True).encode(),
            key_serializer=lambda key: key.encode() if isinstance(key, str) else key,
        )
        try:
            await producer.start()
        except BaseException:
            # BaseException, not Exception: a cancelled startup — a shutdown signal during boot
            # — must not leak the producer's own background tasks either.
            with contextlib.suppress(Exception):
                await producer.stop()
            raise
        self._producer = producer
        _log.info("events.kafka_connected", bootstrap=self._bootstrap)

    async def aclose(self) -> None:
        """Flush and close. ``stop()`` drains buffered records before returning."""
        if self._producer is None:
            return
        await self._producer.stop()
        self._producer = None
        _log.info("events.kafka_closed")

    @property
    def is_connected(self) -> bool:
        return self._producer is not None

    # -- EventBus -------------------------------------------------------------------------

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    async def publish(self, event: Event) -> None:
        """Publish one event, waiting for the broker to acknowledge it."""
        if self._producer is None:
            raise RuntimeError("KafkaEventBus.connect() has not been called")
        await self._producer.send_and_wait(
            topic_for(event.type),
            value=event.to_json(),
            key=event.partition_key(),
        )
        _log.debug(
            "events.published",
            event_id=str(event.event_id),
            type=str(event.type),
            correlation_id=event.correlation_id,
        )

    async def publish_many(self, events: list[Event]) -> None:
        """Publish a batch.

        Sends are issued first and awaited afterwards, so the batch costs one round trip rather
        than one per event. **This is not atomic** — a failure partway leaves earlier events
        published. Atomicity across a store and this bus is the transactional outbox, which is
        W7; until then a caller that needs all-or-nothing must not use this method.
        """
        if self._producer is None:
            raise RuntimeError("KafkaEventBus.connect() has not been called")
        futures = [
            await self._producer.send(
                topic_for(event.type), value=event.to_json(), key=event.partition_key()
            )
            for event in events
        ]
        for future in futures:
            await future

    async def publish_message(self, *, topic: str, key: str, value: dict[str, Any]) -> None:
        """Publish a pre-built message to a named topic.

        What the outbox relay uses. Separate from :meth:`publish` because the relay carries a
        row that was serialised when the business transaction committed, not an ``Event`` object
        — and widening ``publish`` to accept both would blur what an ``Event`` is.

        Waits for the acknowledgement, like ``publish``: a relay that returned before the broker
        confirmed would mark the outbox row published and the event could still vanish in a
        failover. The outbox's entire guarantee rests on this call not lying.
        """
        if self._producer is None:
            raise RuntimeError("KafkaEventBus.connect() has not been called")
        await self._producer.send_and_wait(topic, value=value, key=key)

    async def health_check(self) -> dict[str, Any]:
        """Whether the producer has cluster metadata.

        Deliberately does not publish a probe event: a health check that writes puts synthetic
        records into a topic real consumers read.
        """
        if self._producer is None:
            return {"status": "down", "detail": "not connected"}
        try:
            cluster = self._producer.client.cluster
            brokers = len(cluster.brokers())
        except Exception as exc:
            return {"status": "down", "detail": f"{type(exc).__name__}: {exc}"}
        return {"status": "up" if brokers else "degraded", "brokers": brokers}
