"""What sits in the outbox.

The outbox exists because a database write and a Kafka publish cannot be made atomic. Kafka
transactions are Kafka-to-Kafka; they do not span a database. Two-phase commit across Postgres
and Kafka is available in theory and operated by almost nobody. So the write that matters — the
business data *and* the intent to publish — goes into one local transaction, and a separate
relay carries the intent to the broker afterwards.

That converts an unsolvable distributed-transaction problem into an ordinary one: rows in a
table, published at-least-once, with the consumer made idempotent. It is the standard answer and
it is standard because the alternatives do not work.

**Two identifiers, and they are not interchangeable.**

- ``event_id`` (UUID) is the *deduplication* key. It is stable across every retry and every
  redelivery, so a consumer that has seen it can discard the second copy. It is what makes
  at-least-once delivery safe.
- ``sequence_id`` (monotonic integer, assigned by the store) is the *ordering* key. UUIDs do not
  order, and ``created_at`` cannot be trusted for ordering because two rows written in the same
  millisecond by different connections have no defined relative order — and clock skew across
  replicas makes timestamps actively wrong.

**``partition_key`` is the ordering scope.** Events sharing one are delivered in sequence order;
events with different keys have no relative guarantee and do not need one. It maps directly to
the Kafka partition key, which is what makes the guarantee survive the broker.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = ["OutboxEvent", "OutboxStatus", "PublishAttempt"]


class OutboxStatus(StrEnum):
    """Where one event is in its life.

    There is deliberately **no ``publishing`` status.** A row claimed for publication is held by
    a row lock (``FOR UPDATE SKIP LOCKED``) rather than marked in a column, because a status
    column set to "publishing" by a process that then crashes leaves a row nothing will ever
    reclaim — it is not pending, so no publisher takes it, and it is not published, so nothing
    completes it. A lock is released by the database when the connection dies. A column is not.
    """

    PENDING = "pending"
    """Waiting to be published, or waiting out a backoff after a transient failure."""
    PUBLISHED = "published"
    """Acknowledged by the broker. Terminal."""
    DEAD = "dead"
    """Exhausted its attempts and moved to the dead-letter topic. Terminal until replayed."""


@dataclass(frozen=True, slots=True)
class PublishAttempt:
    """One recorded attempt. Kept so an operator can see *why* something is stuck."""

    attempted_at: dt.datetime
    ok: bool
    error: str = ""
    duration_ms: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "attemptedAt": self.attempted_at.isoformat(),
            "ok": self.ok,
            "error": self.error,
            "durationMs": round(self.duration_ms, 2),
        }


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    """One event, written in the same transaction as the data that caused it."""

    event_type: str
    tenant_id: uuid.UUID
    partition_key: str
    payload: dict[str, Any] = field(default_factory=dict)

    event_id: uuid.UUID = field(default_factory=uuid.uuid4)
    sequence_id: int = 0
    """Assigned by the store on write. Zero means "not yet persisted"."""

    aggregate_type: str = ""
    aggregate_id: str = ""
    """What the event is *about*. Not required for delivery, and invaluable during an incident:
    "show me every event for this patient" is the first question asked and cannot be answered
    from a payload blob."""

    correlation_id: str = ""
    traceparent: str = ""
    """W3C trace context, carried on the row. An asynchronous consumer that starts a fresh trace
    cannot be linked back to the request that caused the event, and the link is exactly what an
    incident investigation needs."""

    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = 0
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    next_attempt_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    published_at: dt.datetime | None = None
    last_error: str = ""

    def __post_init__(self) -> None:
        if not self.event_type.strip():
            raise ValueError("event_type is required")
        if not self.partition_key.strip():
            # Refused rather than defaulted. A blank key would put every event of that kind on
            # one partition, silently serialising the whole platform's throughput behind a
            # single consumer — and it would look like a performance problem, not a bug.
            raise ValueError("partition_key is required; it is the ordering scope")

    def to_json(self) -> dict[str, Any]:
        return {
            "eventId": str(self.event_id),
            "sequenceId": self.sequence_id,
            "eventType": self.event_type,
            "tenantId": str(self.tenant_id),
            "partitionKey": self.partition_key,
            "aggregateType": self.aggregate_type,
            "aggregateId": self.aggregate_id,
            "correlationId": self.correlation_id,
            "traceparent": self.traceparent,
            "status": str(self.status),
            "attempts": self.attempts,
            "createdAt": self.created_at.isoformat(),
            "publishedAt": self.published_at.isoformat() if self.published_at else None,
            "lastError": self.last_error,
            "payload": self.payload,
        }

    def message(self) -> dict[str, Any]:
        """The body published to the broker.

        Carries ``eventId`` so a consumer can deduplicate, and the trace context so it can join
        the producer's trace rather than starting an orphan.
        """
        return {
            "eventId": str(self.event_id),
            "eventType": self.event_type,
            "tenantId": str(self.tenant_id),
            "aggregateType": self.aggregate_type,
            "aggregateId": self.aggregate_id,
            "correlationId": self.correlation_id,
            "traceparent": self.traceparent,
            "occurredAt": self.created_at.isoformat(),
            "sequenceId": self.sequence_id,
            "payload": self.payload,
        }
