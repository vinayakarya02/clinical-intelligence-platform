"""The clinical event stream.

Kafka-shaped: topics, partitions, offsets, consumer groups, committed positions, and replay.
In-memory here; the semantics are the contract a Kafka-backed implementation must satisfy, and
they are tested rather than assumed.

Three decisions carry it (docs/design/adr-0029-event-ordering.md).

**Partitioned by resolved person.** Every event about one patient is totally ordered; across
patients there is none, and nothing may depend on one. A discharge that overtakes its own
admission produces a patient discharged from an encounter that never started.

**Ordered by source sequence, not wall clock.** Sending systems' clocks disagree, sometimes by
hours. A later message with an earlier timestamp is normal, and a consumer that sorts on it will
apply a stale update over a fresh one.

**At-least-once delivery, idempotent consumers.** Exactly-once is implemented where it can
actually be implemented — in the consumer, as a processed-event ledger. A consumer that cannot
state its idempotency key is not registered.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections import OrderedDict, deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_core.logging import get_logger
from cip_interop.domain import InteropError

__all__ = [
    "ClinicalEventType",
    "ConsumerGroup",
    "EventStream",
    "IdempotencyLedger",
    "OrderingViolation",
    "StreamRecord",
    "StreamingError",
    "partition_for",
]

_log = get_logger(__name__)


class StreamingError(InteropError):
    """A streaming operation is not valid."""


class ClinicalEventType(StrEnum):
    """The clinical lifecycle.

    A closed set. A consumer subscribing to a typo'd event name subscribes to silence, and
    nothing about that failure is visible until somebody asks why a downstream view is empty.
    """

    PATIENT_ADMITTED = "PatientAdmitted"
    PATIENT_DISCHARGED = "PatientDischarged"
    PATIENT_TRANSFERRED = "PatientTransferred"
    PATIENT_MERGED = "PatientMerged"
    LAB_ORDER_PLACED = "LabOrderPlaced"
    LAB_RESULT_COMPLETED = "LabResultCompleted"
    IMAGING_ORDER_PLACED = "ImagingOrderPlaced"
    IMAGING_STUDY_AVAILABLE = "ImagingStudyAvailable"
    MEDICATION_UPDATED = "MedicationUpdated"
    DOCUMENT_IMPORTED = "DocumentImported"
    APPOINTMENT_SCHEDULED = "AppointmentScheduled"
    REFERRAL_INITIATED = "ReferralInitiated"
    WORKFLOW_COMPLETED = "WorkflowCompleted"
    DECISION_GENERATED = "DecisionGenerated"
    NOTIFICATION_SENT = "NotificationSent"
    CONSENT_CHANGED = "ConsentChanged"

    @property
    def carries_phi(self) -> bool:
        """Whether a payload of this type may contain protected health information.

        Marked on the type rather than checked per publish, so a new event type has to make the
        decision explicitly instead of inheriting a permissive default.
        """
        return self not in (
            ClinicalEventType.WORKFLOW_COMPLETED,
            ClinicalEventType.NOTIFICATION_SENT,
        )

    @property
    def marks_identity_seam(self) -> bool:
        """Whether this event changes what the partition contains.

        Only a merge does. Two people who become one have two histories that were each ordered
        and are not ordered relative to each other; the merge event marks that seam so a
        replaying consumer can see that what precedes it is a union rather than a sequence.
        """
        return self is ClinicalEventType.PATIENT_MERGED


def partition_for(key: str, partition_count: int) -> int:
    """Which partition a key belongs to.

    A stable digest, **never Python's** ``hash()``. Python randomises string hashing per
    process, so ``hash()`` would put the same patient in different partitions in different
    processes — which silently destroys the per-patient ordering this whole design rests on,
    and only shows up under multi-process deployment.
    """
    if partition_count < 1:
        raise StreamingError("partition_count must be at least 1")
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % partition_count


@dataclass(frozen=True, slots=True)
class StreamRecord:
    """One record on the stream."""

    event_id: str
    event_type: ClinicalEventType
    partition_key: str
    """The resolved person id. Everything about one patient shares a partition."""
    payload: dict[str, Any] = field(default_factory=dict)
    organization_id: str = ""
    source_system: str = ""
    source_sequence: int = 0
    """The sending system's own monotonic sequence. Ordering compares this, never
    ``occurred_at``."""
    occurred_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    """Retained for display. **Never** used for ordering — it is a timestamp from a machine
    this platform does not administer."""
    correlation_id: str = ""
    causation_id: str = ""
    traceparent: str = ""
    partition: int = 0
    offset: int = -1

    def audit_summary(self) -> dict[str, Any]:
        """What may be written to an audit log or telemetry.

        PHI payloads are summarised to their keys, never their values. An audit log is queried
        by operators and retained for years; putting clinical content in it creates a second,
        less-guarded copy of the record.
        """
        return {
            "event_id": self.event_id,
            "type": str(self.event_type),
            "organization_id": self.organization_id,
            "partition": self.partition,
            "offset": self.offset,
            "correlation_id": self.correlation_id,
            "payload_keys": sorted(self.payload) if self.event_type.carries_phi else self.payload,
        }


@dataclass(frozen=True, slots=True)
class OrderingViolation:
    """A source sequence that went backwards or skipped.

    Reported, never silently reordered or buffered away. A gap means a message was lost in
    transit, which is an interface incident — treating it as something to smooth over hides a
    clinical fact that never arrived.
    """

    partition_key: str
    source_system: str
    expected_after: int
    observed: int
    event_id: str

    @property
    def kind(self) -> str:
        return "regression" if self.observed <= self.expected_after else "gap"

    def render(self) -> str:
        return (
            f"{self.kind}: {self.source_system} sequence {self.observed} after "
            f"{self.expected_after} for {self.partition_key}"
        )


class EventStream:
    """A partitioned, replayable log."""

    def __init__(self, *, partitions: int = 12, retention_per_partition: int = 100_000) -> None:
        if partitions < 1:
            raise StreamingError("a stream needs at least one partition")
        self._partitions = partitions
        self._log: list[deque[StreamRecord]] = [
            deque(maxlen=retention_per_partition) for _ in range(partitions)
        ]
        self._base_offset = [0] * partitions
        """Offsets are absolute and monotonic even after retention trimming, so a committed
        offset never silently points at a different record than it did when committed."""
        self._next_offset = [0] * partitions
        self._sequences: dict[tuple[str, str], int] = {}
        self._violations: list[OrderingViolation] = []
        self._retention = retention_per_partition

    @property
    def partition_count(self) -> int:
        return self._partitions

    def publish(
        self,
        event_type: ClinicalEventType,
        *,
        partition_key: str,
        payload: dict[str, Any] | None = None,
        organization_id: str = "",
        source_system: str = "",
        source_sequence: int = 0,
        correlation_id: str = "",
        causation_id: str = "",
        traceparent: str = "",
        occurred_at: dt.datetime | None = None,
        event_id: str = "",
    ) -> StreamRecord:
        """Append a record.

        A source sequence that regresses or skips is **recorded as a violation and still
        appended**. Dropping it would silently lose a clinical fact; reordering it would require
        buffering of unbounded duration. Surfacing it is the only honest option.
        """
        if not partition_key.strip():
            raise StreamingError(
                "a record needs a partition key. Without one there is no per-patient ordering, "
                "which is the only ordering guarantee this stream makes."
            )
        partition = partition_for(partition_key, self._partitions)

        if source_system and source_sequence:
            key = (source_system, partition_key)
            previous = self._sequences.get(key)
            if previous is not None and source_sequence != previous + 1:
                violation = OrderingViolation(
                    partition_key=partition_key,
                    source_system=source_system,
                    expected_after=previous,
                    observed=source_sequence,
                    event_id=event_id or "",
                )
                self._violations.append(violation)
                _log.warning("stream.ordering_violation", detail=violation.render())
            self._sequences[key] = max(source_sequence, previous or 0)

        offset = self._next_offset[partition]
        record = StreamRecord(
            event_id=event_id or f"evt:{uuid.uuid4()}",
            event_type=event_type,
            partition_key=partition_key,
            payload=payload or {},
            organization_id=organization_id,
            source_system=source_system,
            source_sequence=source_sequence,
            occurred_at=occurred_at or dt.datetime.now(dt.UTC),
            correlation_id=correlation_id or f"corr:{uuid.uuid4()}",
            causation_id=causation_id,
            traceparent=traceparent,
            partition=partition,
            offset=offset,
        )
        log = self._log[partition]
        if len(log) == self._retention:
            self._base_offset[partition] += 1
        log.append(record)
        self._next_offset[partition] = offset + 1
        return record

    def caused_by(
        self, parent: StreamRecord, event_type: ClinicalEventType, **payload: Any
    ) -> StreamRecord:
        """A follow-on record inheriting correlation and trace context.

        Exists because a handler that must remember to copy four fields will eventually copy
        three, and the resulting record is orphaned from its trace with no error anywhere.
        """
        return self.publish(
            event_type,
            partition_key=parent.partition_key,
            payload=payload,
            organization_id=parent.organization_id,
            source_system=parent.source_system,
            correlation_id=parent.correlation_id,
            causation_id=parent.event_id,
            traceparent=parent.traceparent,
        )

    def read(
        self, partition: int, *, from_offset: int = 0, limit: int = 100
    ) -> tuple[StreamRecord, ...]:
        if not 0 <= partition < self._partitions:
            raise StreamingError(f"partition {partition} does not exist")
        log = self._log[partition]
        base = self._base_offset[partition]
        if from_offset < base:
            # The requested offset has fallen out of retention. Reading from the earliest
            # surviving record instead would silently skip data; the caller is told.
            raise StreamingError(
                f"offset {from_offset} on partition {partition} is below the retained base "
                f"{base}; those records have been trimmed and cannot be replayed"
            )
        start = from_offset - base
        return tuple(list(log)[start : start + limit])

    def read_key(self, partition_key: str, *, limit: int = 1000) -> tuple[StreamRecord, ...]:
        """Every record for one patient, in order.

        The ordering guarantee made visible: this is a filter over one partition, never a merge
        across several.
        """
        partition = partition_for(partition_key, self._partitions)
        return tuple(r for r in list(self._log[partition]) if r.partition_key == partition_key)[
            :limit
        ]

    def high_water_mark(self, partition: int) -> int:
        return self._next_offset[partition]

    def total_records(self) -> int:
        return sum(len(log) for log in self._log)

    def ordering_violations(self) -> tuple[OrderingViolation, ...]:
        return tuple(self._violations)

    def partition_depths(self) -> dict[int, int]:
        """Records per partition.

        Skew is expected and is monitored rather than fixed: an ICU stay generating continuous
        observations is one patient, one partition, and cannot be parallelised without giving
        up the ordering that matters clinically.
        """
        return {i: len(log) for i, log in enumerate(self._log)}


@dataclass(slots=True)
class IdempotencyLedger:
    """Records which events a consumer has already processed.

    This is where exactly-once actually lives. At-least-once is the only delivery guarantee an
    infrastructure makes cheaply, so duplicates are certain and the consumer must be able to
    recognise one.

    Bounded, with the bound documented as a **redelivery window** rather than a memory setting:
    a duplicate arriving after more events than the ledger holds will be reprocessed.
    """

    capacity: int = 200_000
    _seen: OrderedDict[str, None] = field(default_factory=OrderedDict)

    def seen(self, event_id: str) -> bool:
        return event_id in self._seen

    def remember(self, event_id: str) -> None:
        self._seen[event_id] = None
        self._seen.move_to_end(event_id)
        while len(self._seen) > self.capacity:
            self._seen.popitem(last=False)

    def size(self) -> int:
        return len(self._seen)


class ConsumerGroup:
    """A named consumer with committed offsets.

    Offsets are committed **after** processing, never before. Committing first turns a crash
    mid-processing into silent data loss, which is the failure nobody notices; committing after
    turns it into a duplicate, which the idempotency ledger absorbs.
    """

    def __init__(
        self,
        group_id: str,
        stream: EventStream,
        *,
        idempotency_capacity: int = 200_000,
    ) -> None:
        if not group_id.strip():
            raise StreamingError("a consumer group needs an id")
        self._group_id = group_id
        self._stream = stream
        self._committed: dict[int, int] = {}
        self._ledger = IdempotencyLedger(capacity=idempotency_capacity)
        self._processed = 0
        self._duplicates = 0

    @property
    def group_id(self) -> str:
        return self._group_id

    @property
    def duplicates_suppressed(self) -> int:
        return self._duplicates

    @property
    def processed_count(self) -> int:
        return self._processed

    def committed_offset(self, partition: int) -> int:
        return self._committed.get(partition, 0)

    def poll(self, partition: int, *, limit: int = 100) -> tuple[StreamRecord, ...]:
        return self._stream.read(
            partition, from_offset=self.committed_offset(partition), limit=limit
        )

    def poll_all(self, *, limit_per_partition: int = 100) -> Iterator[StreamRecord]:
        """Records from every partition.

        Deliberately yields partition by partition rather than interleaving by timestamp. An
        interleaved view would imply a cross-partition ordering that does not exist, and a
        consumer would come to depend on it.
        """
        for partition in range(self._stream.partition_count):
            yield from self.poll(partition, limit=limit_per_partition)

    def consume(self, record: StreamRecord) -> bool:
        """Mark a record processed. Returns ``False`` if it was a duplicate."""
        if self._ledger.seen(record.event_id):
            self._duplicates += 1
            return False
        self._ledger.remember(record.event_id)
        self._processed += 1
        return True

    def commit(self, partition: int, offset: int) -> None:
        """Advance the committed position.

        Refuses to move backwards. A rewind would re-deliver everything since, which the
        ledger would suppress — so the observable effect would be a consumer that appears to
        run and processes nothing.
        """
        current = self._committed.get(partition, 0)
        if offset < current:
            raise StreamingError(
                f"cannot commit offset {offset} below the current committed {current} on "
                f"partition {partition}; use seek() to replay deliberately"
            )
        self._committed[partition] = offset

    def seek(self, partition: int, offset: int, *, replay_processed: bool = False) -> None:
        """Deliberately reposition, for reprocessing after a mapping fix.

        ``replay_processed`` clears the idempotency ledger. Without it a replay delivers records
        the ledger suppresses, so the replay silently does nothing — which is the confusing
        failure people hit when reprocessing after fixing a mapping bug.
        """
        self._committed[partition] = max(0, offset)
        if replay_processed:
            self._ledger = IdempotencyLedger(capacity=self._ledger.capacity)

    def lag(self) -> dict[int, int]:
        """Uncommitted records per partition. The number an alert watches."""
        return {
            partition: self._stream.high_water_mark(partition) - self.committed_offset(partition)
            for partition in range(self._stream.partition_count)
        }

    def total_lag(self) -> int:
        return sum(self.lag().values())
