"""HL7 v2 parsing.

A v2 message declares its own delimiters in ``MSH-1`` and ``MSH-2``. This parser reads them
from the message being parsed rather than assuming the conventional ``|^~\\&``
(docs/design/adr-0025-hl7-parsing.md). The tempting ``line.split("|")`` works on every message
anyone tests with and fails silently on the first non-conventional sender: every field after
the divergence shifts by one, so a collection time is read as a result value and nothing raises.

Four properties are load-bearing, each preventing a real defect class:

- Delimiters come from the message, so field alignment survives an unusual sender.
- Escape sequences are decoded **on access**, so ``Smith \\T\\ Sons`` is one field containing an
  ampersand rather than two subcomponents, and the raw form survives for retransmission.
- Repetitions are a list at every level. A repeating ``PID-3`` never collapses to its first
  identifier, which is how a patient's second medical record number gets lost.
- Unknown and ``Z`` segments are retained verbatim and in order.

The MSH off-by-one is handled once, here, and tested: ``MSH-1`` *is* the field separator, so
for MSH alone field *n* lives at split index *n-1*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from cip_interop.domain import ValidationError

__all__ = [
    "Delimiters",
    "Hl7Message",
    "Hl7ParseError",
    "Segment",
    "parse_message",
]

#: Segment separator. The standard says carriage return; real senders emit LF and CRLF too, and
#: refusing those would reject traffic that every production interface engine accepts.
_SEGMENT_SPLIT = re.compile(r"\r\n|\r|\n")

_HEX_ESCAPE = re.compile(r"^X([0-9A-Fa-f]+)$")


class Hl7ParseError(ValidationError):
    """A message could not be parsed.

    Carries the segment and field where parsing failed so the ``AR`` acknowledgement can name
    it. An interface that rejects without saying where is one nobody can fix.
    """

    def __init__(self, message: str, *, segment: str = "", position: int | None = None) -> None:
        super().__init__(message)
        self.segment = segment
        self.position = position

    def describe(self) -> str:
        where = self.segment or "message"
        if self.position is not None:
            where = f"{where}-{self.position}"
        return f"{where}: {self.args[0]}"


@dataclass(frozen=True, slots=True)
class Delimiters:
    """The encoding characters this message uses.

    Read from ``MSH-1`` and ``MSH-2`` of the message being parsed, never assumed.
    """

    field: str = "|"
    component: str = "^"
    repetition: str = "~"
    escape: str = "\\"
    subcomponent: str = "&"
    truncation: str = ""
    """v2.7+ truncation character. Empty when the sender did not declare one — most do not."""

    @classmethod
    def from_msh(cls, raw: str) -> Delimiters:
        """Read the delimiter set from a raw message.

        ``MSH-1`` and ``MSH-2`` are the only two fields whose position is knowable without
        already knowing the delimiters, which is why they are read positionally from the raw
        text before anything is split.
        """
        if len(raw) < 8 or not raw.startswith("MSH"):
            raise Hl7ParseError(
                "message does not begin with an MSH segment", segment="MSH", position=0
            )
        separator = raw[3]
        if separator.isalnum() or separator.isspace():
            raise Hl7ParseError(
                f"MSH-1 field separator {separator!r} is alphanumeric or whitespace, which "
                "cannot delimit fields",
                segment="MSH",
                position=1,
            )
        encoding = ""
        for char in raw[4:]:
            if char == separator:
                break
            encoding += char
        if len(encoding) < 4:
            raise Hl7ParseError(
                f"MSH-2 declares {len(encoding)} encoding characters; at least 4 are required "
                "(component, repetition, escape, subcomponent)",
                segment="MSH",
                position=2,
            )
        declared = [separator, *encoding[:5]]
        if len(set(declared)) != len(declared):
            raise Hl7ParseError(
                f"delimiter set {declared!r} contains a repeated character, so the message "
                "cannot be unambiguously split",
                segment="MSH",
                position=2,
            )
        return cls(
            field=separator,
            component=encoding[0],
            repetition=encoding[1],
            escape=encoding[2],
            subcomponent=encoding[3],
            truncation=encoding[4] if len(encoding) > 4 else "",
        )

    def decode(self, text: str) -> str:
        """Decode escape sequences to their literal characters.

        Applied on access rather than at tokenise time, so the raw form survives for
        retransmission byte-identical to how it arrived.

        An unrecognised escape is left **verbatim** rather than dropped. Dropping it silently
        deletes content — a locally-defined sequence would vanish from a field a clinician
        reads — and there is no safe substitute to invent.
        """
        if self.escape not in text:
            return text

        out: list[str] = []
        index = 0
        length = len(text)
        while index < length:
            char = text[index]
            if char != self.escape:
                out.append(char)
                index += 1
                continue
            close = text.find(self.escape, index + 1)
            if close == -1:
                # An unterminated escape. Keep it as written; the alternative is to consume the
                # rest of the field into an escape that never ends.
                out.append(text[index:])
                break
            code = text[index + 1 : close]
            out.append(self._expand(code))
            index = close + 1
        return "".join(out)

    def _expand(self, code: str) -> str:
        simple = {
            "F": self.field,
            "S": self.component,
            "T": self.subcomponent,
            "R": self.repetition,
            "E": self.escape,
        }
        if code in simple:
            return simple[code]
        if hex_match := _HEX_ESCAPE.match(code):
            digits = hex_match.group(1)
            if len(digits) % 2:
                return f"{self.escape}{code}{self.escape}"
            try:
                return bytes.fromhex(digits).decode("utf-8", errors="replace")
            except ValueError:  # pragma: no cover - guarded by the regex
                return f"{self.escape}{code}{self.escape}"
        if code == ".br":
            return "\n"
        if code in ("H", "N", ".sp", ".fi", ".nf"):
            # Display formatting. Rendered as nothing rather than as text — emitting ".sp"
            # into a clinical field would put markup in front of a clinician.
            return ""
        return f"{self.escape}{code}{self.escape}"


#: Nesting: field -> repetition -> component -> subcomponent.
Component = tuple[str, ...]
Repetition = tuple[Component, ...]
FieldValue = tuple[Repetition, ...]


@dataclass(frozen=True, slots=True)
class Segment:
    """One parsed segment.

    ``fields`` is indexed from 1 in HL7 terms; :meth:`get` does the translation, including the
    MSH special case where field *n* lives at split index *n-1* because ``MSH-1`` is the field
    separator itself.
    """

    name: str
    fields: tuple[FieldValue, ...]
    delimiters: Delimiters
    raw: str = ""
    set_id: str = ""

    @property
    def is_z_segment(self) -> bool:
        """Whether this is a locally-defined segment.

        Retained verbatim. Retaining is not understanding — the mapping layer must be told what
        a local segment means before anything reads it.
        """
        return self.name.startswith("Z")

    def field_count(self) -> int:
        """The highest field number present."""
        return len(self.fields) + (1 if self.name == "MSH" else 0)

    def _index(self, number: int) -> int:
        if number < 1:
            raise Hl7ParseError(
                f"field number {number} is below 1; HL7 fields are numbered from 1",
                segment=self.name,
                position=number,
            )
        return number - 2 if self.name == "MSH" else number - 1

    def raw_field(self, number: int) -> FieldValue:
        """The parsed field, undecoded, as nested repetitions/components/subcomponents."""
        if self.name == "MSH" and number == 1:
            return (((self.delimiters.field,),),)
        index = self._index(number)
        if index < 0 or index >= len(self.fields):
            return ()
        return self.fields[index]

    def get(
        self, number: int, *, repeat: int = 0, component: int = 1, subcomponent: int = 1
    ) -> str:
        """One decoded value.

        ``component`` and ``subcomponent`` are 1-based, matching how HL7 specifications and
        interface documents are written — ``PID-5.1`` is the family name. ``repeat`` is 0-based
        because it is an index into a list rather than an HL7 coordinate, and conflating the
        two conventions is how the wrong repeat gets read.

        A missing field, repeat, component, or subcomponent returns ``""``. Absent and empty
        are the same thing to a consumer; distinguishing them would push HL7's null-flavour
        problem into every caller.
        """
        value = self.raw_field(number)
        if repeat < 0 or repeat >= len(value):
            return ""
        components = value[repeat]
        if component < 1 or component > len(components):
            return ""
        subcomponents = components[component - 1]
        if subcomponent < 1 or subcomponent > len(subcomponents):
            return ""
        return self.delimiters.decode(subcomponents[subcomponent - 1])

    def repeats(self, number: int) -> tuple[Repetition, ...]:
        """Every repetition of a field.

        The reason repetitions are never collapsed: ``PID-3`` routinely carries a medical
        record number *and* a national identifier, and a parser that returns the first one
        loses whichever the sender happened to put second.
        """
        return self.raw_field(number)

    def repeat_count(self, number: int) -> int:
        return len(self.raw_field(number))

    def components(self, number: int, *, repeat: int = 0) -> tuple[str, ...]:
        """Every decoded component of one repetition."""
        value = self.raw_field(number)
        if repeat < 0 or repeat >= len(value):
            return ()
        return tuple(self.delimiters.decode(sub[0]) if sub else "" for sub in value[repeat])

    def to_json(self) -> dict[str, Any]:
        return {
            "segment": self.name,
            "fields": self.field_count(),
            "z_segment": self.is_z_segment,
        }


@dataclass(frozen=True, slots=True)
class Hl7Message:
    """A parsed message.

    Holds the raw text and the delimiter set as well as the parsed segments, so a message can
    be forwarded byte-identical to how it arrived — integration engines need this, because the
    downstream system may care about something this platform chose not to model.
    """

    segments: tuple[Segment, ...]
    delimiters: Delimiters
    raw: str = ""
    unparsed_lines: tuple[str, ...] = ()
    """Lines that did not begin with a plausible segment name. Retained rather than dropped so
    the reject acknowledgement can quote them."""

    @property
    def header(self) -> Segment:
        return self.segments[0]

    @property
    def message_type(self) -> str:
        """``MSH-9.1``, the message code — ADT, ORU, ORM, SIU, DFT, ACK."""
        return self.header.get(9, component=1)

    @property
    def trigger_event(self) -> str:
        """``MSH-9.2``, the trigger event — A01, R01, O01."""
        return self.header.get(9, component=2)

    @property
    def message_structure(self) -> str:
        """``MSH-9.3``. Often absent; callers must not require it."""
        return self.header.get(9, component=3)

    @property
    def control_id(self) -> str:
        """``MSH-10``. The sender's own identifier for this message, echoed in the ACK."""
        return self.header.get(10)

    @property
    def version(self) -> str:
        """``MSH-12``."""
        return self.header.get(12)

    @property
    def sending_application(self) -> str:
        return self.header.get(3)

    @property
    def sending_facility(self) -> str:
        return self.header.get(4)

    @property
    def receiving_application(self) -> str:
        return self.header.get(5)

    @property
    def receiving_facility(self) -> str:
        return self.header.get(6)

    @property
    def timestamp(self) -> str:
        """``MSH-7``, as the sender wrote it.

        Deliberately a string. It is a timestamp from a machine this platform does not
        administer, and it is **never** used for ordering
        (docs/design/adr-0029-event-ordering.md).
        """
        return self.header.get(7)

    @property
    def processing_id(self) -> str:
        """``MSH-11``. ``P`` production, ``D`` debugging, ``T`` training."""
        return self.header.get(11, component=1)

    @property
    def is_production(self) -> bool:
        """Whether this message claims to be production traffic.

        A test message landing in a production repository is a data-integrity incident, so the
        routing layer checks this rather than assuming.
        """
        return self.processing_id.upper() == "P"

    def named(self, name: str) -> tuple[Segment, ...]:
        """Every segment with this name, in order."""
        wanted = name.upper()
        return tuple(s for s in self.segments if s.name == wanted)

    def first(self, name: str) -> Segment | None:
        found = self.named(name)
        return found[0] if found else None

    def segment_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.segments)

    def z_segments(self) -> tuple[Segment, ...]:
        return tuple(s for s in self.segments if s.is_z_segment)

    def to_json(self) -> dict[str, Any]:
        return {
            "message_type": self.message_type,
            "trigger_event": self.trigger_event,
            "control_id": self.control_id,
            "version": self.version,
            "sending_application": self.sending_application,
            "sending_facility": self.sending_facility,
            "processing_id": self.processing_id,
            "segments": list(self.segment_names()),
            "z_segments": [s.name for s in self.z_segments()],
        }


_SEGMENT_NAME = re.compile(r"^[A-Z][A-Z0-9]{2}$")


def _split_field(text: str, delimiters: Delimiters) -> FieldValue:
    """Split one field into repetitions, components, and subcomponents.

    No decoding happens here. Escape sequences do not contain raw delimiter characters — that
    is what they are for — so splitting before decoding is safe, and decoding after splitting
    is what makes an escaped delimiter survive as a literal.
    """
    repetitions: list[Repetition] = []
    for repetition in text.split(delimiters.repetition):
        components: list[Component] = []
        for component in repetition.split(delimiters.component):
            components.append(tuple(component.split(delimiters.subcomponent)))
        repetitions.append(tuple(components))
    return tuple(repetitions)


def _parse_segment(line: str, delimiters: Delimiters) -> Segment:
    parts = line.split(delimiters.field)
    name = parts[0].strip()
    if not _SEGMENT_NAME.match(name):
        raise Hl7ParseError(
            f"segment name {name!r} is not three uppercase alphanumerics", segment=name
        )
    # Splitting is identical for every segment; the MSH difference is in *addressing*, not in
    # tokenising. MSH-1 is the field separator itself, so the encoding-characters field landing
    # at index 0 is MSH-2 and field n is at index n-2. Every other segment's field n is at
    # index n-1. That single off-by-one is the most common HL7 parser defect, so it lives in
    # exactly one place (Segment._index) and is tested directly.
    raw_fields = parts[1:]
    if name == "MSH" and raw_fields:
        # MSH-2 *contains* the component, repetition, and subcomponent separators, so splitting
        # it on them shreds it into nonsense. The standard treats it literally; so does this.
        fields = ((((raw_fields[0],),),), *(_split_field(p, delimiters) for p in raw_fields[1:]))
    else:
        fields = tuple(_split_field(part, delimiters) for part in raw_fields)
    set_id = ""
    if name != "MSH" and fields and fields[0] and fields[0][0] and fields[0][0][0]:
        set_id = fields[0][0][0][0]
    return Segment(name=name, fields=fields, delimiters=delimiters, raw=line, set_id=set_id)


def parse_message(raw: str | bytes, *, encoding: str = "utf-8") -> Hl7Message:
    """Parse a v2 message.

    Raises :class:`Hl7ParseError` rather than returning a partial message. An interface that
    acknowledges what it did not understand loses data silently, which is worse than one that
    visibly stops.
    """
    if isinstance(raw, bytes):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError as exc:
            raise Hl7ParseError(
                f"message is not valid {encoding}: {exc.reason}. MLLP carries single-byte "
                "encodings, UTF-8, and Shift_JIS; UTF-16 conflicts with the framing bytes."
            ) from exc
    else:
        text = raw

    text = text.strip("\x0b\x1c\r\n")
    if not text:
        raise Hl7ParseError("message is empty")

    delimiters = Delimiters.from_msh(text)

    segments: list[Segment] = []
    unparsed: list[str] = []
    for line in _SEGMENT_SPLIT.split(text):
        if not line.strip():
            continue
        try:
            segments.append(_parse_segment(line, delimiters))
        except Hl7ParseError:
            # A malformed line does not abort the message, but it is never silently dropped:
            # it is retained so the acknowledgement can quote it and an operator can see what
            # arrived.
            unparsed.append(line)

    if not segments or segments[0].name != "MSH":
        raise Hl7ParseError("no parseable MSH segment", segment="MSH")

    return Hl7Message(
        segments=tuple(segments),
        delimiters=delimiters,
        raw=text,
        unparsed_lines=tuple(unparsed),
    )


@dataclass(slots=True)
class MessageBuilder:
    """Builds a message with a chosen delimiter set.

    Used for acknowledgements and for outbound messages. Encoding is the inverse of decoding:
    literal delimiter characters in a value are escaped, so a name containing an ampersand does
    not become two subcomponents at the receiver.
    """

    delimiters: Delimiters = dc_field(default_factory=Delimiters)
    _segments: list[list[str]] = dc_field(default_factory=list)

    def escape(self, value: str) -> str:
        """Escape delimiter characters in a value.

        The escape character is replaced first. Doing it in any other order would re-escape the
        backslashes introduced by the other replacements.
        """
        d = self.delimiters
        out = value.replace(d.escape, f"{d.escape}E{d.escape}")
        out = out.replace(d.field, f"{d.escape}F{d.escape}")
        out = out.replace(d.component, f"{d.escape}S{d.escape}")
        out = out.replace(d.repetition, f"{d.escape}R{d.escape}")
        return out.replace(d.subcomponent, f"{d.escape}T{d.escape}")

    def segment(self, name: str, *fields: str, escape: bool = True) -> MessageBuilder:
        values = [self.escape(f) if escape else f for f in fields]
        self._segments.append([name, *values])
        return self

    def render(self) -> str:
        """The message, segments separated by carriage return as the standard requires."""
        return "\r".join(self.delimiters.field.join(s) for s in self._segments)
