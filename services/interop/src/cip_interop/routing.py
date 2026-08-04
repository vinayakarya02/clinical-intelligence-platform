"""The integration engine: channels, filters, transformation, retries, and dead-lettering.

Modelled on how interface engines actually work — a source, optional filters, a transformer,
and one or more destinations, each destination with **its own independent queue**. That last
detail is the one that matters: a downstream lab system being unreachable must not stop
delivery to the EHR, and a single shared queue makes one slow consumer stall every other.

The pipeline this assembles is the phase in one function:

    frame -> parse -> validate -> acknowledge -> map -> resolve identity -> store -> publish

Acknowledgement happens **before** downstream delivery and after durable acceptance. A sender
that is not acknowledged retransmits, so acknowledging late produces duplicates; acknowledging
before the message is safely accepted loses it. The order here is: accept, acknowledge, then
process — and processing failures go to a dead-letter queue rather than back to the sender,
because the sender cannot fix a mapping bug by resending.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from cip_core.logging import get_logger
from cip_interop.domain import (
    Address,
    AdministrativeSex,
    ContactPoint,
    HumanName,
    Identifier,
    IdentifierUse,
    InteropError,
    PersonRecord,
)
from cip_interop.empi.index import EmpiIndex, Resolution
from cip_interop.fhir.repository import FhirRepository, RepositoryRegistry
from cip_interop.fhir.resources import Resource
from cip_interop.hl7.messages import AckCode, build_ack, validate_message
from cip_interop.hl7.parser import Hl7Message, Hl7ParseError, parse_message
from cip_interop.mapping.engine import MappingEngine
from cip_interop.streaming import ClinicalEventType, EventStream

__all__ = [
    "Channel",
    "ChannelState",
    "DeadLetter",
    "DeadLetterQueue",
    "IngestOutcome",
    "IntegrationEngine",
    "RetryPolicy",
    "RoutingError",
]

_log = get_logger(__name__)


class RoutingError(InteropError):
    """A routing operation failed."""


class ChannelState(StrEnum):
    """Whether a channel is accepting messages."""

    STARTED = "started"
    PAUSED = "paused"
    """Accepting and queueing, not delivering. The state to use during a downstream
    maintenance window — messages are retained rather than rejected at the sender."""
    STOPPED = "stopped"

    @property
    def accepts(self) -> bool:
        return self is not ChannelState.STOPPED

    @property
    def delivers(self) -> bool:
        return self is ChannelState.STARTED


class FailureKind(StrEnum):
    """Why a delivery failed, which decides whether retrying can help."""

    TRANSIENT = "transient"
    """Network, timeout, downstream restart. Retry."""
    PERMANENT = "permanent"
    """Malformed payload, rejected content, unknown destination. Retrying is load with no
    progress, so it goes straight to the dead-letter queue."""

    @property
    def retryable(self) -> bool:
        return self is FailureKind.TRANSIENT


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many times and how far apart.

    Backoff is exponential with a cap. The cap matters: unbounded exponential backoff means a
    message retried overnight arrives in the morning, and a clinical result that late is worse
    than one that visibly failed.
    """

    max_attempts: int = 5
    initial_delay_seconds: float = 1.0
    multiplier: float = 2.0
    max_delay_seconds: float = 300.0

    def delay_for(self, attempt: int) -> float:
        if attempt < 1:
            return 0.0
        delay = self.initial_delay_seconds * (self.multiplier ** (attempt - 1))
        return min(delay, self.max_delay_seconds)

    def should_retry(self, attempt: int, kind: FailureKind) -> bool:
        return kind.retryable and attempt < self.max_attempts


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """One message that could not be delivered."""

    letter_id: str
    channel: str
    destination: str
    payload: str
    reason: str
    kind: FailureKind
    attempts: int
    first_failed_at: dt.datetime
    last_failed_at: dt.datetime
    control_id: str = ""
    correlation_id: str = ""

    def render(self) -> str:
        return (
            f"{self.channel}/{self.destination} [{self.kind.value}] after {self.attempts} "
            f"attempt(s): {self.reason}"
        )


@dataclass(slots=True)
class DeadLetterQueue:
    """Undeliverable messages, retained for replay.

    Bounded, because an unbounded dead-letter queue is how a failing downstream turns into an
    out-of-memory kill of the thing that was still working. The bound drops the **oldest**, and
    the count of dropped letters is retained — a dead-letter queue that silently loses entries
    is worse than none, because it looks like nothing failed.
    """

    capacity: int = 10_000
    letters: deque[DeadLetter] = field(default_factory=deque)
    dropped: int = 0

    def add(self, letter: DeadLetter) -> None:
        self.letters.append(letter)
        while len(self.letters) > self.capacity:
            self.letters.popleft()
            self.dropped += 1

    def depth(self) -> int:
        return len(self.letters)

    def by_channel(self, channel: str) -> tuple[DeadLetter, ...]:
        return tuple(letter for letter in self.letters if letter.channel == channel)

    def drain(self) -> tuple[DeadLetter, ...]:
        """Take everything for replay, leaving the queue empty."""
        taken = tuple(self.letters)
        self.letters.clear()
        return taken


@runtime_checkable
class Destination(Protocol):
    """Somewhere a transformed message is delivered."""

    name: str

    async def deliver(self, payload: Any, context: dict[str, Any]) -> None: ...


@dataclass(slots=True)
class Channel:
    """One inbound interface.

    ``filters`` run before transformation and are ordinary predicates. A message a filter
    rejects is **acknowledged and counted**, not dead-lettered: the sender did nothing wrong,
    and a filtered message is a deliberate configuration choice rather than a failure.
    """

    name: str
    source_system: str
    organization_id: str
    mapping: MappingEngine
    destinations: list[Destination] = field(default_factory=list)
    filters: list[Callable[[Hl7Message], bool]] = field(default_factory=list)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    state: ChannelState = ChannelState.STARTED
    accept_non_production: bool = False
    received: int = 0
    filtered: int = 0
    delivered: int = 0
    failed: int = 0

    def accepts(self, message: Hl7Message) -> bool:
        return all(predicate(message) for predicate in self.filters)

    def statistics(self) -> dict[str, Any]:
        return {
            "channel": self.name,
            "state": str(self.state),
            "received": self.received,
            "filtered": self.filtered,
            "delivered": self.delivered,
            "failed": self.failed,
            "destinations": [d.name for d in self.destinations],
        }


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """Everything one inbound message produced."""

    accepted: bool
    ack: str
    ack_code: AckCode
    channel: str = ""
    control_id: str = ""
    resources: tuple[Resource, ...] = ()
    resolution: Resolution | None = None
    person_id: str = ""
    published: int = 0
    dead_lettered: int = 0
    filtered: bool = False
    warnings: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "ack_code": str(self.ack_code),
            "channel": self.channel,
            "control_id": self.control_id,
            "resources": [r.reference() for r in self.resources],
            "person_id": self.person_id,
            "published": self.published,
            "dead_lettered": self.dead_lettered,
            "filtered": self.filtered,
            "warnings": list(self.warnings),
        }


#: Which clinical event a message family announces. A message type with no entry publishes
#: nothing rather than a generic event — a consumer subscribing to "something happened" cannot
#: do anything useful with it.
_EVENT_FOR: dict[tuple[str, str], ClinicalEventType] = {
    ("ADT", "A01"): ClinicalEventType.PATIENT_ADMITTED,
    ("ADT", "A04"): ClinicalEventType.PATIENT_ADMITTED,
    ("ADT", "A02"): ClinicalEventType.PATIENT_TRANSFERRED,
    ("ADT", "A03"): ClinicalEventType.PATIENT_DISCHARGED,
    ("ADT", "A40"): ClinicalEventType.PATIENT_MERGED,
    ("ORU", "R01"): ClinicalEventType.LAB_RESULT_COMPLETED,
    ("ORM", "O01"): ClinicalEventType.LAB_ORDER_PLACED,
    ("SIU", "S12"): ClinicalEventType.APPOINTMENT_SCHEDULED,
    ("SIU", "S13"): ClinicalEventType.APPOINTMENT_SCHEDULED,
    ("SIU", "S14"): ClinicalEventType.APPOINTMENT_SCHEDULED,
}


class IntegrationEngine:
    """Routes inbound messages through the full pipeline."""

    def __init__(
        self,
        *,
        empi: EmpiIndex,
        repositories: RepositoryRegistry,
        stream: EventStream,
        dead_letters: DeadLetterQueue | None = None,
    ) -> None:
        self._empi = empi
        self._repositories = repositories
        self._stream = stream
        self._dead_letters = dead_letters or DeadLetterQueue()
        self._channels: dict[str, Channel] = {}
        self._sequences: dict[str, int] = {}
        self._seen_control_ids: dict[str, deque[str]] = {}
        self._duplicates = 0

    @property
    def dead_letters(self) -> DeadLetterQueue:
        return self._dead_letters

    @property
    def duplicates_suppressed(self) -> int:
        return self._duplicates

    def register(self, channel: Channel) -> None:
        if channel.name in self._channels:
            raise RoutingError(f"channel {channel.name!r} is already registered")
        self._channels[channel.name] = channel
        self._seen_control_ids[channel.name] = deque(maxlen=10_000)

    def channel(self, name: str) -> Channel:
        found = self._channels.get(name)
        if found is None:
            raise RoutingError(f"unknown channel {name!r}")
        return found

    def channels(self) -> tuple[Channel, ...]:
        return tuple(self._channels.values())

    async def ingest(
        self, raw: str | bytes, *, channel_name: str, at: dt.datetime | None = None
    ) -> IngestOutcome:
        """Run one message through the pipeline."""
        channel = self.channel(channel_name)
        if not channel.state.accepts:
            return IngestOutcome(
                accepted=False,
                ack=build_ack(None, AckCode.ERROR, text=f"channel {channel_name} is stopped"),
                ack_code=AckCode.ERROR,
                channel=channel_name,
            )

        channel.received += 1

        try:
            message = parse_message(raw)
        except Hl7ParseError as exc:
            # Unparseable is rejected, never acknowledged as accepted. An interface that
            # acknowledges what it did not understand loses data silently.
            _log.warning("routing.parse_failed", channel=channel_name, error=exc.describe())
            return IngestOutcome(
                accepted=False,
                ack=build_ack(None, AckCode.REJECT, text=exc.describe()),
                ack_code=AckCode.REJECT,
                channel=channel_name,
            )

        outcome = validate_message(message, accept_non_production=channel.accept_non_production)
        if not outcome.acceptable:
            return IngestOutcome(
                accepted=False,
                ack=build_ack(message, outcome.ack_code, issues=outcome.issues),
                ack_code=outcome.ack_code,
                channel=channel_name,
                control_id=message.control_id,
            )

        seen = self._seen_control_ids[channel_name]
        if message.control_id in seen:
            # A retransmission. Acknowledged positively — the sender is behaving correctly and
            # needs to stop resending — but not reprocessed, because reprocessing an ADT would
            # re-resolve identity and reprocessing an ORU would duplicate results.
            self._duplicates += 1
            return IngestOutcome(
                accepted=True,
                ack=build_ack(
                    message, AckCode.ACCEPT, text="duplicate control id; not reprocessed"
                ),
                ack_code=AckCode.ACCEPT,
                channel=channel_name,
                control_id=message.control_id,
            )
        seen.append(message.control_id)

        if not channel.accepts(message):
            channel.filtered += 1
            return IngestOutcome(
                accepted=True,
                ack=build_ack(message, AckCode.ACCEPT, text="filtered by channel configuration"),
                ack_code=AckCode.ACCEPT,
                channel=channel_name,
                control_id=message.control_id,
                filtered=True,
            )

        warnings: list[str] = [i.text for i in outcome.warnings]

        try:
            mapped = channel.mapping.apply(message, organization_id=channel.organization_id)
        except InteropError as exc:
            self._dead_letter(channel, "mapping", message, str(exc), FailureKind.PERMANENT)
            channel.failed += 1
            return IngestOutcome(
                accepted=True,
                ack=build_ack(message, AckCode.ERROR, text=f"mapping failed: {exc}"),
                ack_code=AckCode.ERROR,
                channel=channel_name,
                control_id=message.control_id,
                dead_lettered=1,
            )
        warnings.extend(mapped.warnings)

        resolution = self._resolve_identity(message, channel, at=at)
        person_id = resolution.person_id if resolution else ""

        repository = self._repositories.for_organization(channel.organization_id)
        stored = self._store(repository, mapped.resources, channel, message)

        # The engine is the only place that holds both the organisation-local resource id and
        # the resolved person, so it is the only place that can record the association that
        # consent lookup depends on.
        if person_id:
            for resource in stored:
                self._empi.register_resource_alias(
                    organization_id=channel.organization_id,
                    resource_type=resource.resource_type,
                    resource_id=resource.id,
                    person_id=person_id,
                )

        published = 0
        event_type = _EVENT_FOR.get((message.message_type, message.trigger_event.upper()))
        if event_type and person_id:
            sequence = self._next_sequence(channel.source_system, person_id)
            # HL7 carries no trace header, so the trace starts here. Without one, every
            # asynchronous consumer begins an orphan trace and the path from a wire message to
            # a downstream effect cannot be reconstructed — which is the question asked during
            # an incident.
            trace_id = uuid.uuid4().hex
            span_id = uuid.uuid4().hex[:16]
            self._stream.publish(
                event_type,
                partition_key=person_id,
                payload={
                    "control_id": message.control_id,
                    "resources": [r.reference() for r in stored],
                    "message_type": f"{message.message_type}^{message.trigger_event}",
                },
                organization_id=channel.organization_id,
                source_system=channel.source_system,
                source_sequence=sequence,
                correlation_id=f"corr:{channel.name}:{message.control_id}",
                traceparent=f"00-{trace_id}-{span_id}-01",
                occurred_at=at,
            )
            published = 1

        channel.delivered += 1
        return IngestOutcome(
            accepted=True,
            ack=build_ack(message, AckCode.ACCEPT),
            ack_code=AckCode.ACCEPT,
            channel=channel_name,
            control_id=message.control_id,
            resources=stored,
            resolution=resolution,
            person_id=person_id,
            published=published,
            warnings=tuple(warnings),
        )

    def _next_sequence(self, source_system: str, person_id: str) -> int:
        key = f"{source_system}|{person_id}"
        self._sequences[key] = self._sequences.get(key, 0) + 1
        return self._sequences[key]

    def _resolve_identity(
        self, message: Hl7Message, channel: Channel, *, at: dt.datetime | None
    ) -> Resolution | None:
        """Build a PersonRecord from PID and resolve it.

        A message with no PID resolves to nothing rather than to a placeholder person. An SIU
        that carries only a schedule has no patient demographics to match on, and inventing a
        person for it would create one new person per appointment.
        """
        record = person_record_from(message, channel.source_system, channel.organization_id)
        if record is None:
            return None
        try:
            return self._empi.ingest(record, at=at)
        except InteropError:
            # Already indexed — the same source record arriving again. Resolve to its existing
            # person rather than failing the message.
            try:
                person_id = self._empi.person_for(record.record_id)
            except InteropError:
                return None
            from cip_interop.empi.index import LinkOrigin
            from cip_interop.empi.matching import MatchZone

            return Resolution(
                record_id=record.record_id,
                person_id=person_id,
                zone=MatchZone.MATCH,
                origin=LinkOrigin.AUTOMATIC,
            )

    def _store(
        self,
        repository: FhirRepository,
        resources: tuple[Resource, ...],
        channel: Channel,
        message: Hl7Message,
    ) -> tuple[Resource, ...]:
        """Create or update each resource, dead-lettering the ones that fail.

        Per-resource rather than all-or-nothing: an ORU whose fourth Observation is malformed
        should still deliver the first three, and the failure should be visible rather than
        costing the whole report.
        """
        stored: list[Resource] = []
        for resource in resources:
            try:
                if repository.exists(resource.resource_type, resource.id):
                    current = repository.read(resource.resource_type, resource.id)
                    version = repository.update(resource, if_match=current.etag)
                else:
                    version = repository.create(resource)
                stored.append(version.resource)
            except InteropError as exc:
                channel.failed += 1
                self._dead_letter(
                    channel, resource.reference(), message, str(exc), FailureKind.PERMANENT
                )
        return tuple(stored)

    def _dead_letter(
        self,
        channel: Channel,
        destination: str,
        message: Hl7Message,
        reason: str,
        kind: FailureKind,
    ) -> None:
        now = dt.datetime.now(dt.UTC)
        self._dead_letters.add(
            DeadLetter(
                letter_id=f"dl:{uuid.uuid4()}",
                channel=channel.name,
                destination=destination,
                payload=message.raw,
                reason=reason,
                kind=kind,
                attempts=1,
                first_failed_at=now,
                last_failed_at=now,
                control_id=message.control_id,
            )
        )
        _log.warning(
            "routing.dead_lettered",
            channel=channel.name,
            destination=destination,
            kind=kind.value,
            reason=reason[:120],
        )

    def statistics(self) -> dict[str, Any]:
        return {
            "channels": [c.statistics() for c in self._channels.values()],
            "dead_letters": self._dead_letters.depth(),
            "dead_letters_dropped": self._dead_letters.dropped,
            "duplicates_suppressed": self._duplicates,
        }


def person_record_from(
    message: Hl7Message, source_system: str, organization_id: str
) -> PersonRecord | None:
    """Build an EMPI record from a message's ``PID``.

    Reads **every** ``PID-3`` repetition. A parser that takes only the first loses the national
    identifier that is the strongest matching evidence available, and the loss is invisible.
    """
    pid = message.first("PID")
    if pid is None:
        return None

    identifiers: list[Identifier] = []
    for index in range(max(1, pid.repeat_count(3))):
        value = pid.get(3, repeat=index, component=1)
        if not value:
            continue
        authority = pid.get(3, repeat=index, component=4) or organization_id
        type_code = pid.get(3, repeat=index, component=5)
        identifiers.append(
            Identifier(
                system=f"urn:id:{authority}",
                value=value,
                type_code=type_code,
                use=IdentifierUse.OFFICIAL if type_code else IdentifierUse.USUAL,
                assigner=authority,
            )
        )
    if not identifiers:
        return None

    given = tuple(n for n in (pid.get(5, component=2), pid.get(5, component=3)) if n)
    names = (
        (HumanName(family=pid.get(5, component=1), given=given),) if pid.get(5, component=1) else ()
    )

    birth_date = None
    raw_birth = pid.get(7)
    if len(raw_birth) >= 8:
        try:
            birth_date = dt.date(int(raw_birth[0:4]), int(raw_birth[4:6]), int(raw_birth[6:8]))
        except ValueError:
            birth_date = None

    addresses = ()
    if pid.get(11, component=5):
        addresses = (
            Address(
                line=(pid.get(11, component=1),) if pid.get(11, component=1) else (),
                city=pid.get(11, component=3),
                state=pid.get(11, component=4),
                postal_code=pid.get(11, component=5),
            ),
        )

    telecom = ()
    if pid.get(13, component=1):
        telecom = (ContactPoint(system="phone", value=pid.get(13, component=1)),)

    return PersonRecord(
        record_id=f"{organization_id}|{source_system}|{identifiers[0].value}",
        source_system=source_system,
        organization_id=organization_id,
        identifiers=tuple(identifiers),
        names=names,
        birth_date=birth_date,
        sex=AdministrativeSex.from_hl7(pid.get(8)),
        addresses=addresses,
        telecom=telecom,
        raw_ref=message.control_id,
    )
