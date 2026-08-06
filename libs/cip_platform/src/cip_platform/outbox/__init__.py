"""The transactional outbox.

A database write and a broker publish cannot be made atomic. The outbox makes them *one local
transaction* plus a relay, which is the standard answer because the alternatives — Kafka
transactions (Kafka-to-Kafka only) and two-phase commit (operated by almost nobody) — do not
solve it.
"""

from cip_platform.outbox.models import OutboxEvent, OutboxStatus, PublishAttempt
from cip_platform.outbox.publisher import OutboxPublisher, PublisherConfig, PublisherStats
from cip_platform.outbox.store import (
    ClaimedBatch,
    InMemoryOutboxStore,
    OutboxStats,
    OutboxStore,
)

__all__ = [
    "ClaimedBatch",
    "InMemoryOutboxStore",
    "OutboxEvent",
    "OutboxPublisher",
    "OutboxStats",
    "OutboxStatus",
    "OutboxStore",
    "PublishAttempt",
    "PublisherConfig",
    "PublisherStats",
]
