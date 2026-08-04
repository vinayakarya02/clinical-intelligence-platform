"""Domain types for interoperability.

Imports nothing else from this package — the base of the dependency order, enforced by a test.

Three design points carry the phase.

:class:`Identifier` always carries the **system that issued it**. A bare identifier string is
the root cause of cross-organisation patient mixups: "12345" means one person at one hospital
and someone else at another, and a value without its issuing system cannot be compared safely.

:class:`PurposeOfUse` is a required parameter of every disclosure, never a default. A purpose
the system infers is a purpose nobody stated
(docs/design/adr-0028-consent-deny-by-default.md).

:class:`PersonRecord` is a *source* record, not a person. One human has many; deciding which
records are one person is the EMPI's job and it is allowed to be uncertain.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "Address",
    "AdministrativeSex",
    "ContactPoint",
    "HumanName",
    "Identifier",
    "IdentifierUse",
    "InteropError",
    "MappingError",
    "PersonRecord",
    "PurposeOfUse",
    "SourceSystem",
    "ValidationError",
    "normalise_name_token",
]


class InteropError(Exception):
    """Base for every failure in this service."""


class ValidationError(InteropError):
    """Input did not conform to the standard it claimed to follow."""


class MappingError(InteropError):
    """A mapping could not be applied."""


class IdentifierUse(StrEnum):
    """What an identifier is for.

    ``TEMP`` matters more than it looks: a temporary identifier (a trauma-bay pseudonym, an
    unidentified-patient number) must never be used as EMPI matching evidence, because two
    different unidentified patients can be issued the same one.
    """

    USUAL = "usual"
    OFFICIAL = "official"
    SECONDARY = "secondary"
    TEMP = "temp"
    OLD = "old"

    @property
    def is_matching_evidence(self) -> bool:
        """Whether the EMPI may use this identifier to link records."""
        return self in (IdentifierUse.USUAL, IdentifierUse.OFFICIAL, IdentifierUse.SECONDARY)


@dataclass(frozen=True, slots=True)
class Identifier:
    """A business identifier, always qualified by its issuing system.

    ``system`` is mandatory and non-empty. Comparing two identifiers without it is the single
    most common way records for different people get merged: medical record number 12345 exists
    at almost every hospital on earth, and it is a different person at each one.
    """

    system: str
    value: str
    use: IdentifierUse = IdentifierUse.USUAL
    type_code: str = ""
    """The identifier type — MR, SS, DL, PPN, NI. Populated from HL7 ``PID-3.5`` or the FHIR
    identifier type coding, and used to decide whether an identifier is nationally unique."""
    assigner: str = ""
    period_start: dt.date | None = None
    period_end: dt.date | None = None

    def __post_init__(self) -> None:
        if not self.system.strip():
            raise ValidationError(
                "Identifier.system must not be empty. An identifier without its issuing "
                "system cannot be compared across organisations."
            )
        if not self.value.strip():
            raise ValidationError("Identifier.value must not be empty")

    @property
    def key(self) -> tuple[str, str]:
        """The comparison key. Both halves, always."""
        return (self.system.strip().lower(), self.value.strip())

    @property
    def is_nationally_unique(self) -> bool:
        """Whether a match on this identifier is strong evidence of the same person.

        Only identifier types issued by a national authority qualify. A medical record number
        is unique *within* an assigner and says nothing across one.
        """
        return self.type_code.upper() in {"SS", "SSN", "NI", "NH", "PPN"}

    def is_active(self, on: dt.date) -> bool:
        if self.use is IdentifierUse.OLD:
            return False
        if self.period_start and on < self.period_start:
            return False
        return not (self.period_end and on > self.period_end)

    def render(self) -> str:
        return f"{self.system}|{self.value}"


def normalise_name_token(token: str) -> str:
    """Fold a name token for comparison.

    Case, accents, punctuation, and surrounding whitespace are removed. Accent folding is the
    one worth naming: "Muller" and "Müller" are the same person written by two registration
    clerks with different keyboards, and a matcher that treats them as disagreeing will split
    that person's record at every organisational boundary.

    This is *comparison* normalisation. The original is always retained — a system that
    displays the folded form has corrupted the name.
    """
    decomposed = unicodedata.normalize("NFKD", token)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", stripped.lower())


@dataclass(frozen=True, slots=True)
class HumanName:
    """A name as one source system recorded it."""

    family: str = ""
    given: tuple[str, ...] = ()
    prefix: str = ""
    suffix: str = ""
    use: str = "official"

    @property
    def comparison_family(self) -> str:
        return normalise_name_token(self.family)

    @property
    def comparison_given(self) -> tuple[str, ...]:
        return tuple(normalise_name_token(g) for g in self.given if normalise_name_token(g))

    def render(self) -> str:
        parts = [p for p in (self.prefix, " ".join(self.given), self.family, self.suffix) if p]
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class Address:
    """A postal address."""

    line: tuple[str, ...] = ()
    city: str = ""
    district: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = ""
    use: str = "home"

    @property
    def comparison_postal(self) -> str:
        """Postal code folded for comparison.

        Truncated to the first five characters, so a US ZIP+4 compares equal to the same ZIP
        without the extension — the extension is a delivery-route detail that varies between
        systems recording the same address.
        """
        return re.sub(r"[^A-Za-z0-9]", "", self.postal_code).upper()[:5]


@dataclass(frozen=True, slots=True)
class ContactPoint:
    """A phone number, email, or similar."""

    system: str
    value: str
    use: str = ""

    @property
    def comparison_value(self) -> str:
        """Digits only for phone, lowercase for everything else.

        Phone formatting varies wildly between systems recording the same number; the digits do
        not. Only the last ten digits are compared, so a number with a country code matches the
        same number without one.
        """
        if self.system.lower() in ("phone", "fax", "sms", "pager"):
            digits = re.sub(r"\D", "", self.value)
            return digits[-10:] if len(digits) >= 10 else digits
        return self.value.strip().lower()


class AdministrativeSex(StrEnum):
    """Administrative sex as recorded, which is not the same as clinical sex or gender.

    ``UNKNOWN`` participates in no matching comparison. Treating unknown as a disagreement
    would penalise every record from a system that does not collect it.
    """

    MALE = "male"
    FEMALE = "female"
    OTHER = "other"
    UNKNOWN = "unknown"

    @classmethod
    def from_hl7(cls, code: str) -> AdministrativeSex:
        """Map an HL7 ``PID-8`` administrative sex code.

        An unrecognised code becomes ``UNKNOWN`` rather than raising: sex is not a field worth
        rejecting an admission message over, and unknown is the honest reading of a code this
        platform does not know.
        """
        return {
            "M": cls.MALE,
            "F": cls.FEMALE,
            "O": cls.OTHER,
            "A": cls.OTHER,
            "N": cls.OTHER,
            "U": cls.UNKNOWN,
            "": cls.UNKNOWN,
        }.get(code.strip().upper(), cls.UNKNOWN)


class PurposeOfUse(StrEnum):
    """Why a disclosure is being made.

    HL7 v3 ``ActReason`` codes. A required parameter of every disclosure — never defaulted,
    because a purpose the system infers is a purpose nobody stated
    (docs/design/adr-0028-consent-deny-by-default.md).
    """

    TREATMENT = "TREAT"
    EMERGENCY_TREATMENT = "ETREAT"
    PAYMENT = "HPAYMT"
    OPERATIONS = "HOPERAT"
    RESEARCH = "HRESCH"
    PUBLIC_HEALTH = "PUBHLTH"
    PATIENT_REQUEST = "PATRQT"
    MARKETING = "HMARKT"
    BREAK_GLASS = "BTG"

    @property
    def requires_named_human(self) -> bool:
        """Whether a service account may invoke this purpose.

        Break-glass and emergency treatment may not. Both exist to be answered for afterwards,
        and a service account cannot be asked why it did something.
        """
        return self in (PurposeOfUse.BREAK_GLASS, PurposeOfUse.EMERGENCY_TREATMENT)

    @property
    def is_break_glass(self) -> bool:
        return self is PurposeOfUse.BREAK_GLASS


@dataclass(frozen=True, slots=True)
class SourceSystem:
    """A system this platform receives data from.

    ``sequence_origin`` names where ordering comes from for this source. Recorded per source
    because ordering is a property of the sender, not of the network
    (docs/design/adr-0029-event-ordering.md).
    """

    system_id: str
    organization_id: str
    name: str = ""
    facility: str = ""
    sequence_origin: str = "control_id"
    contact: str = ""

    def __post_init__(self) -> None:
        if not self.system_id.strip():
            raise ValidationError("SourceSystem.system_id must not be empty")
        if not self.organization_id.strip():
            raise ValidationError(
                f"SourceSystem '{self.system_id}' has no organization. A source whose owning "
                "organisation is unknown cannot be tenant-scoped."
            )


@dataclass(frozen=True, slots=True)
class PersonRecord:
    """Demographics as **one source system** recorded them.

    Deliberately not called ``Patient``. This is a record, and a person has many; deciding
    which records describe one person is the EMPI's job, and it is allowed to be uncertain
    (docs/design/adr-0027-empi-review-not-automerge.md).
    """

    record_id: str
    source_system: str
    organization_id: str
    identifiers: tuple[Identifier, ...] = ()
    names: tuple[HumanName, ...] = ()
    birth_date: dt.date | None = None
    sex: AdministrativeSex = AdministrativeSex.UNKNOWN
    addresses: tuple[Address, ...] = ()
    telecom: tuple[ContactPoint, ...] = ()
    deceased: bool = False
    received_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    raw_ref: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.record_id.strip():
            raise ValidationError("PersonRecord.record_id must not be empty")
        if not self.organization_id.strip():
            raise ValidationError(
                f"PersonRecord '{self.record_id}' has no organization. An unattributed record "
                "cannot be tenant-scoped, and a record nobody owns is one anybody can read."
            )

    @property
    def primary_name(self) -> HumanName | None:
        """The name to compare against.

        Prefers an official name; falls back to the first recorded. Never merges parts from
        several names — a person with a maiden and a married name has two names, and a
        Frankenstein of both matches neither.
        """
        for name in self.names:
            if name.use == "official":
                return name
        return self.names[0] if self.names else None

    def matching_identifiers(self, on: dt.date | None = None) -> tuple[Identifier, ...]:
        """Identifiers the EMPI may use as evidence."""
        today = on or dt.date.today()
        return tuple(
            i for i in self.identifiers if i.use.is_matching_evidence and i.is_active(today)
        )

    def identifier_for(self, system: str) -> Identifier | None:
        wanted = system.strip().lower()
        for identifier in self.identifiers:
            if identifier.system.strip().lower() == wanted:
                return identifier
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "source_system": self.source_system,
            "organization_id": self.organization_id,
            "identifiers": [i.render() for i in self.identifiers],
            "name": self.primary_name.render() if self.primary_name else "",
            "birth_date": self.birth_date.isoformat() if self.birth_date else None,
            "sex": str(self.sex),
        }


def new_record_id(prefix: str = "rec") -> str:
    """A record id for a record that arrived without one."""
    return f"{prefix}:{uuid.uuid4()}"
