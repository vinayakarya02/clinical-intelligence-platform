"""Message types, validation, and acknowledgement.

The five families that carry most hospital interface traffic — ADT, ORM, ORU, SIU, DFT — plus
ACK. Each declares which segments it requires, so a message missing one is rejected with the
segment named rather than silently producing a resource with no subject.

The acknowledgement codes are three and they mean different things to a sender:

``AA`` accept — processed. ``AE`` error — the message was understood and something about
processing it failed; the sender may fix and resend. ``AR`` reject — the message was not
understood or is not acceptable; resending it unchanged will fail again.

Collapsing ``AE`` and ``AR`` is a real operational cost: a sending system's retry logic uses
them to decide whether retrying is pointless, and a receiver that always says ``AE`` gets
retried forever.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_interop.hl7.parser import Delimiters, Hl7Message, MessageBuilder, Segment

__all__ = [
    "AckCode",
    "MessageProfile",
    "MessageType",
    "ValidationIssue",
    "ValidationOutcome",
    "build_ack",
    "profile_for",
    "validate_message",
]


class MessageType(StrEnum):
    """The message families this platform accepts."""

    ADT = "ADT"
    """Admit, discharge, transfer — registration and patient movement."""
    ORM = "ORM"
    """Order message — clinical orders to lab, radiology, pharmacy."""
    ORU = "ORU"
    """Observation result, unsolicited — results and diagnostic reports."""
    SIU = "SIU"
    """Scheduling information, unsolicited — appointments."""
    DFT = "DFT"
    """Detailed financial transaction — charge capture."""
    ACK = "ACK"
    """General acknowledgement."""

    @property
    def carries_clinical_results(self) -> bool:
        return self is MessageType.ORU


class AckCode(StrEnum):
    """Acknowledgement codes, with the distinction senders actually act on."""

    ACCEPT = "AA"
    ERROR = "AE"
    REJECT = "AR"

    @property
    def sender_should_retry(self) -> bool:
        """Whether resending the identical message could succeed.

        ``AE`` yes — the failure was on this side, or transient. ``AR`` no — the message itself
        is unacceptable, and a sender that retries it is generating load and no progress.
        """
        return self is AckCode.ERROR


@dataclass(frozen=True, slots=True)
class MessageProfile:
    """What a message family must contain.

    Deliberately minimal: the segments without which the message cannot be mapped to anything.
    Over-strict profiles are how interfaces reject valid traffic from senders whose optional
    segments differ, and every rejection is a clinical fact that did not arrive.
    """

    message_type: MessageType
    required_segments: tuple[str, ...]
    #: Segments required only for specific trigger events, keyed by trigger.
    required_for_trigger: dict[str, tuple[str, ...]] = field(default_factory=dict)
    description: str = ""


_PROFILES: dict[MessageType, MessageProfile] = {
    MessageType.ADT: MessageProfile(
        message_type=MessageType.ADT,
        required_segments=("MSH", "EVN", "PID"),
        required_for_trigger={
            "A01": ("PV1",),
            "A02": ("PV1",),
            "A03": ("PV1",),
            "A04": ("PV1",),
            # A40 is a merge. MRG carries the identifier being merged away, and a merge
            # message without it names only the survivor — which would silently do nothing.
            "A40": ("MRG",),
            "A34": ("MRG",),
            "A44": ("MRG",),
            "A47": ("MRG",),
        },
        description="Registration and patient movement",
    ),
    MessageType.ORM: MessageProfile(
        message_type=MessageType.ORM,
        required_segments=("MSH", "PID", "ORC"),
        description="Clinical orders",
    ),
    MessageType.ORU: MessageProfile(
        message_type=MessageType.ORU,
        required_segments=("MSH", "PID", "OBR"),
        description="Observation results",
    ),
    MessageType.SIU: MessageProfile(
        message_type=MessageType.SIU,
        required_segments=("MSH", "SCH"),
        description="Appointment scheduling",
    ),
    MessageType.DFT: MessageProfile(
        message_type=MessageType.DFT,
        required_segments=("MSH", "EVN", "PID", "FT1"),
        description="Financial transactions",
    ),
    MessageType.ACK: MessageProfile(
        message_type=MessageType.ACK,
        required_segments=("MSH", "MSA"),
        description="Acknowledgement",
    ),
}


def profile_for(message_type: str) -> MessageProfile | None:
    """The profile for a message code, or ``None`` if unsupported."""
    try:
        return _PROFILES[MessageType(message_type.upper())]
    except (ValueError, KeyError):
        return None


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One reason a message is not acceptable."""

    severity: str
    """``error`` blocks processing; ``warning`` does not."""
    text: str
    segment: str = ""
    position: int | None = None

    @property
    def location(self) -> str:
        if self.segment and self.position is not None:
            return f"{self.segment}-{self.position}"
        return self.segment or "MSH"

    def render(self) -> str:
        return f"[{self.severity}] {self.location}: {self.text}"


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    """Everything validation concluded."""

    issues: tuple[ValidationIssue, ...] = ()
    message_type: str = ""
    trigger_event: str = ""

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "error")

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "warning")

    @property
    def acceptable(self) -> bool:
        return not self.errors

    @property
    def ack_code(self) -> AckCode:
        """The acknowledgement this outcome warrants.

        Structural failures are ``AR``: the sender must change the message, and retrying it
        unchanged is pointless.
        """
        return AckCode.ACCEPT if self.acceptable else AckCode.REJECT

    def render(self) -> str:
        return "\n".join(i.render() for i in self.issues) or "no issues"


#: MSH fields without which a message cannot be acknowledged, routed, or deduplicated.
_REQUIRED_MSH = (
    (9, "message type"),
    (10, "message control id"),
    (12, "version id"),
)


def validate_message(
    message: Hl7Message, *, accept_non_production: bool = False
) -> ValidationOutcome:
    """Check a parsed message against its profile.

    ``accept_non_production`` defaults to ``False``: a message flagged ``D`` (debugging) or
    ``T`` (training) in ``MSH-11`` is rejected, because test data landing in a production
    clinical repository is a data-integrity incident and the flag is the sender telling you
    exactly that.
    """
    issues: list[ValidationIssue] = []

    for position, label in _REQUIRED_MSH:
        if not message.header.get(position):
            issues.append(
                ValidationIssue(
                    severity="error",
                    text=f"{label} is empty; the message cannot be acknowledged or deduplicated",
                    segment="MSH",
                    position=position,
                )
            )

    for line in message.unparsed_lines:
        issues.append(
            ValidationIssue(
                severity="error",
                text=f"unparseable segment: {line[:60]!r}",
                segment=line[:3].upper() if len(line) >= 3 else "",
            )
        )

    if not accept_non_production and message.processing_id and not message.is_production:
        issues.append(
            ValidationIssue(
                severity="error",
                text=(
                    f"processing id {message.processing_id!r} is not production; refusing to "
                    "admit test data to a clinical repository"
                ),
                segment="MSH",
                position=11,
            )
        )

    profile = profile_for(message.message_type)
    if profile is None:
        issues.append(
            ValidationIssue(
                severity="error",
                text=(
                    f"message type {message.message_type!r} is not supported. Supported: "
                    + ", ".join(sorted(t.value for t in MessageType))
                ),
                segment="MSH",
                position=9,
            )
        )
        return ValidationOutcome(
            issues=tuple(issues),
            message_type=message.message_type,
            trigger_event=message.trigger_event,
        )

    present = set(message.segment_names())
    for required in profile.required_segments:
        if required not in present:
            issues.append(
                ValidationIssue(
                    severity="error",
                    text=f"required segment {required} is absent for {profile.message_type}",
                    segment=required,
                )
            )

    for extra in profile.required_for_trigger.get(message.trigger_event.upper(), ()):
        if extra not in present:
            issues.append(
                ValidationIssue(
                    severity="error",
                    text=(
                        f"segment {extra} is required for "
                        f"{profile.message_type}^{message.trigger_event}"
                    ),
                    segment=extra,
                )
            )

    for z_segment in message.z_segments():
        issues.append(
            ValidationIssue(
                severity="warning",
                text=(
                    f"locally-defined segment {z_segment.name} retained but not mapped; "
                    "nothing reads it until a mapping declares what it means"
                ),
                segment=z_segment.name,
            )
        )

    return ValidationOutcome(
        issues=tuple(issues),
        message_type=message.message_type,
        trigger_event=message.trigger_event,
    )


def _timestamp(moment: dt.datetime | None = None) -> str:
    return (moment or dt.datetime.now(dt.UTC)).strftime("%Y%m%d%H%M%S")


def build_ack(
    message: Hl7Message | None,
    code: AckCode,
    *,
    text: str = "",
    issues: tuple[ValidationIssue, ...] = (),
    receiving_application: str = "CIP",
    receiving_facility: str = "CIP",
    control_id: str = "",
    at: dt.datetime | None = None,
) -> str:
    """Build an acknowledgement.

    ``message`` may be ``None`` — a message that could not be parsed still needs an answer, and
    the sender needs to learn that the thing it sent was not understood. In that case the
    echoed control id is empty and ``MSA-3`` says why, which is the honest representation of
    "we could not read your message well enough to identify it".

    Sender and receiver are swapped from the inbound message, which is what makes an ACK
    routable back to its origin.
    """
    delimiters = message.delimiters if message else Delimiters()
    builder = MessageBuilder(delimiters=delimiters)

    inbound_control = message.control_id if message else ""
    sending_app = message.receiving_application if message else receiving_application
    sending_facility = message.receiving_facility if message else receiving_facility
    target_app = message.sending_application if message else ""
    target_facility = message.sending_facility if message else ""
    version = message.version if message else "2.5.1"

    builder.segment(
        # MSH-1 is the field separator itself and is produced by joining, never passed as a
        # value — passing it emits an extra empty field and shifts every field after it by one,
        # which is the same off-by-one the parser handles on the way in.
        "MSH",
        f"{delimiters.component}{delimiters.repetition}{delimiters.escape}"
        f"{delimiters.subcomponent}",
        sending_app or receiving_application,
        sending_facility or receiving_facility,
        target_app,
        target_facility,
        _timestamp(at),
        "",
        f"ACK{delimiters.component}{message.trigger_event if message else ''}",
        control_id or f"ACK{_timestamp(at)}",
        "P",
        version or "2.5.1",
        escape=False,
    )

    detail = text or (issues[0].text if issues else "")
    builder.segment("MSA", str(code), inbound_control, detail[:80])

    for issue in issues:
        if issue.severity != "error":
            continue
        # ERR-2 is the location, ERR-3 the code, ERR-4 the severity. Populating the location is
        # what turns "rejected" into something an integration analyst can act on without
        # asking for the payload, which they often cannot legally be sent.
        builder.segment(
            "ERR",
            "",
            f"{issue.segment}{delimiters.component}1{delimiters.component}{issue.position or ''}",
            "207",
            "E",
            "",
            "",
            issue.text[:200],
            escape=False,
        )

    return builder.render()


@dataclass(frozen=True, slots=True)
class PatientIdentification:
    """A typed view over ``PID``.

    A view, not a copy: it reads the segment on demand rather than snapshotting it, so there is
    no second representation to drift from the first.
    """

    segment: Segment

    @property
    def identifier_repeats(self) -> int:
        return self.segment.repeat_count(3)

    def identifier(self, index: int) -> tuple[str, str, str]:
        """One ``PID-3`` repetition as (value, assigning authority, identifier type).

        ``PID-3`` repeats routinely — a medical record number *and* a national identifier — and
        reading only the first is how the second is lost.
        """
        return (
            self.segment.get(3, repeat=index, component=1),
            self.segment.get(3, repeat=index, component=4),
            self.segment.get(3, repeat=index, component=5),
        )

    @property
    def family_name(self) -> str:
        return self.segment.get(5, component=1)

    @property
    def given_names(self) -> tuple[str, ...]:
        given = self.segment.get(5, component=2)
        middle = self.segment.get(5, component=3)
        return tuple(n for n in (given, middle) if n)

    @property
    def birth_date_raw(self) -> str:
        return self.segment.get(7)

    @property
    def sex_code(self) -> str:
        return self.segment.get(8)

    def to_json(self) -> dict[str, Any]:
        return {
            "identifiers": [self.identifier(i) for i in range(self.identifier_repeats)],
            "family": self.family_name,
            "given": list(self.given_names),
            "birth_date": self.birth_date_raw,
            "sex": self.sex_code,
        }
