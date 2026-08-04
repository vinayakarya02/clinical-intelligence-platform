"""The closed set of mapping transforms.

Mapping files are edited by integration analysts, so they must not be programs
(docs/design/adr-0026-mapping-as-data.md). A transform is chosen **by name** from this table; a
site quirk that no transform expresses becomes a new named transform, reviewed once, rather
than an inline expression that nobody diffs.

The refusal is the same one the rules engine makes about ``eval`` and the HL7 parser makes about
assumed delimiters: a file an operator edits must not be able to execute arbitrary code.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from typing import Any

from cip_interop.domain import AdministrativeSex, MappingError

__all__ = [
    "TRANSFORMS",
    "TransformSpec",
    "hl7_timestamp",
    "transform_names",
]

_HL7_TS = re.compile(r"^(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(?:\.\d+)?([+-]\d{4})?$")
_OFFSET = re.compile(r"^(Z|[+-]\d{2}:\d{2})$")


def hl7_timestamp(value: str, *, want: str = "dateTime", timezone: str = "") -> str:
    """Convert an HL7 ``TS`` to a FHIR date, dateTime, or instant.

    HL7 timestamps are variable precision — ``2026``, ``202603``, ``20260320``,
    ``20260320143000`` are all legal — and FHIR preserves that distinction. Padding a
    date-only value to midnight would assert a time the sender did not send, and "the specimen
    was collected at 00:00" is a clinical claim nobody made.

    **A timestamp carrying a time needs a timezone, and HL7 v2 senders routinely omit one.**
    FHIR requires an offset whenever a ``dateTime`` includes hours, so there are three
    possible behaviours and only one is defensible:

    - assume UTC — shifts every timestamp from that sender by its local offset, silently, and
      a lab drawn at 08:00 becomes one drawn at 03:00
    - drop to date precision — loses the time on a specimen collection, which lab trending needs
    - **require the interface to declare the sending facility's timezone** — which is what a
      real integration engine does, because the offset is a property of the sender that
      somebody knows

    So ``timezone`` is a mapping parameter. When it is absent and the timestamp has a time but
    no offset, this raises, the field is omitted, and the mapping result carries a warning
    naming the interface. That is a loud, fixable configuration gap rather than data that is
    quietly wrong by hours.
    """
    text = value.strip()
    if not text:
        return ""
    match = _HL7_TS.match(text)
    if not match:
        raise MappingError(f"{value!r} is not an HL7 timestamp")
    year, month, day, hour, minute, second, offset = match.groups()

    if want == "date" or not month:
        parts = [year]
        if month:
            parts.append(month)
        if day:
            parts.append(day)
        return "-".join(parts)

    if not day:
        return f"{year}-{month}"
    if hour is None:
        return f"{year}-{month}-{day}"

    stamp = f"{year}-{month}-{day}T{hour}:{minute or '00'}:{second or '00'}"
    if offset:
        return stamp + f"{offset[:3]}:{offset[3:]}"
    if timezone:
        if not _OFFSET.match(timezone):
            raise MappingError(
                f"declared timezone {timezone!r} is not a FHIR offset (Z or +HH:MM / -HH:MM)"
            )
        return stamp + timezone
    raise MappingError(
        f"{value!r} carries a time but no timezone offset, and this mapping declares no "
        "'timezone' parameter. FHIR requires an offset on any dateTime with a time. Set the "
        "sending facility's offset in the mapping rather than letting one be assumed."
    )


def _identity(value: str, params: dict[str, Any]) -> Any:
    return value or None


def _constant(value: str, params: dict[str, Any]) -> Any:
    return params.get("value")


def _code_lookup(value: str, params: dict[str, Any]) -> Any:
    """Map a source code to a target code through a declared table.

    An unmapped code is an **error**, not a pass-through. Passing it through writes a local
    mnemonic into a field that a required binding will later reject, or worse, into one that has
    no binding — where it will be read as though it meant something standard.
    """
    table = params.get("table") or {}
    if not value:
        return None
    if value in table:
        return table[value]
    if params.get("passthrough_unmapped"):
        return value
    raise MappingError(
        f"code {value!r} is not in the lookup table for this mapping and "
        "passthrough_unmapped is not set; an unmapped code written through unchanged would be "
        "read as though it were standard"
    )


def _date(value: str, params: dict[str, Any]) -> Any:
    return hl7_timestamp(value, want="date") or None


def _datetime(value: str, params: dict[str, Any]) -> Any:
    return hl7_timestamp(value, want="dateTime", timezone=str(params.get("timezone", ""))) or None


def _instant(value: str, params: dict[str, Any]) -> Any:
    return hl7_timestamp(value, want="instant", timezone=str(params.get("timezone", ""))) or None


def _sex(value: str, params: dict[str, Any]) -> Any:
    sex = AdministrativeSex.from_hl7(value)
    return sex.value


def _boolean(value: str, params: dict[str, Any]) -> Any:
    true_values = {str(v).upper() for v in params.get("true", ["Y", "YES", "1", "TRUE"])}
    false_values = {str(v).upper() for v in params.get("false", ["N", "NO", "0", "FALSE"])}
    upper = value.strip().upper()
    if upper in true_values:
        return True
    if upper in false_values:
        return False
    return None


def _decimal(value: str, params: dict[str, Any]) -> Any:
    text = value.strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        # A non-numeric result value is common and legitimate — "DETECTED", "<0.5", "TNP".
        # Returning None lets the mapping fall through to a string-valued target rather than
        # inventing a number.
        return None
    return int(number) if number.is_integer() else number


def _reference(value: str, params: dict[str, Any]) -> Any:
    """Build a typed FHIR reference from a source identifier.

    ``resource_type`` and ``id_prefix`` must match the mapping that creates the target
    resource, so the reference resolves to something the same bundle stores. An untyped
    reference is refused by validation precisely so this cannot be left half-done — a bare
    "12345" forces every consumer to guess the type from context.
    """
    resource_type = params.get("resource_type")
    if not resource_type:
        raise MappingError("the 'reference' transform requires a resource_type parameter")
    text = value.strip()
    if not text:
        return None
    identifier = re.sub(r"[^A-Za-z0-9\-.]", "-", f"{params.get('id_prefix', '')}{text}")[:64]
    reference: dict[str, Any] = {"reference": f"{resource_type}/{identifier}"}
    if params.get("display"):
        reference["display"] = params["display"]
    return reference


def _upper(value: str, params: dict[str, Any]) -> Any:
    return value.strip().upper() or None


def _trim(value: str, params: dict[str, Any]) -> Any:
    return value.strip() or None


class TransformSpec:
    """One named transform."""

    def __init__(
        self, name: str, function: Callable[[str, dict[str, Any]], Any], description: str
    ) -> None:
        self.name = name
        self.function = function
        self.description = description

    def apply(self, value: str, params: dict[str, Any] | None = None) -> Any:
        return self.function(value, params or {})


TRANSFORMS: dict[str, TransformSpec] = {
    spec.name: spec
    for spec in (
        TransformSpec("identity", _identity, "Copy the value unchanged"),
        TransformSpec("trim", _trim, "Copy, trimmed"),
        TransformSpec("upper", _upper, "Copy, upper-cased"),
        TransformSpec("constant", _constant, "Write a fixed value, ignoring the source"),
        TransformSpec("code_lookup", _code_lookup, "Map through a declared code table"),
        TransformSpec("date", _date, "HL7 TS to a FHIR date"),
        TransformSpec("datetime", _datetime, "HL7 TS to a FHIR dateTime, precision preserved"),
        TransformSpec("instant", _instant, "HL7 TS to a FHIR instant; refuses a missing offset"),
        TransformSpec("sex", _sex, "HL7 administrative sex to the FHIR gender code"),
        TransformSpec("boolean", _boolean, "Y/N to a FHIR boolean"),
        TransformSpec("decimal", _decimal, "Numeric text to a number, or nothing if not numeric"),
        TransformSpec("reference", _reference, "Source identifier to a typed FHIR reference"),
    )
}


def transform_names() -> tuple[str, ...]:
    return tuple(sorted(TRANSFORMS))


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
