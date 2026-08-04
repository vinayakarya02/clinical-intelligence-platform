"""The HL7-to-FHIR mapping engine.

Loads declarative mapping sets, validates them strictly at load time, and applies them.

The loader refuses four things, each of which is otherwise a silent wrong answer
(docs/design/adr-0026-mapping-as-data.md):

- an **unknown transform name** — a silently unapplied transform produces plausible wrong data
- a **target path that is not a real element** on the target resource — a typo writes a field
  no validator checks and no consumer reads
- a mapping set with **no version** — an interface whose behaviour changed on an unknown date
  cannot be investigated after an incident
- two mappings writing the **same target** without a declared precedence

Coverage is measurable: :meth:`MappingSet.consumed_fields` says which HL7 fields a set reads,
so an operator can ask what arrives that nothing looks at.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from cip_core.logging import get_logger
from cip_interop.domain import MappingError
from cip_interop.fhir.definitions import definition_for
from cip_interop.fhir.resources import Resource
from cip_interop.hl7.parser import Hl7Message
from cip_interop.mapping.transforms import TRANSFORMS, transform_names

__all__ = [
    "FieldMapping",
    "MappingEngine",
    "MappingResult",
    "MappingSet",
    "ResourceMapping",
    "load_mapping_set",
]

_log = get_logger(__name__)

#: ``PID-5.1`` or ``OBX-5`` or ``PID-3.1[*]`` — segment, field, optional component, optional
#: subcomponent, optional repeat marker.
_SOURCE = re.compile(
    r"^(?P<segment>[A-Z][A-Z0-9]{2})-(?P<field>\d+)"
    r"(?:\.(?P<component>\d+))?(?:\.(?P<subcomponent>\d+))?"
    r"(?P<repeat>\[\*\])?$"
)

_TARGET_STEP = re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9]*)(?:\[(?P<index>\d+|\*)\])?$")


@dataclass(frozen=True, slots=True)
class FieldMapping:
    """One source field to one target path."""

    source: str
    target: str
    transform: str = "identity"
    params: dict[str, Any] = field(default_factory=dict)
    required: bool = False
    """When set, an absent source value fails the mapping rather than omitting the target.
    Reserved for fields a resource cannot be valid without — a subject reference, a status."""
    note: str = ""

    @property
    def segment(self) -> str:
        match = _SOURCE.match(self.source)
        return match.group("segment") if match else ""

    @property
    def repeats(self) -> bool:
        return self.source.endswith("[*]")


@dataclass(frozen=True, slots=True)
class ResourceMapping:
    """How one FHIR resource is built from one message."""

    resource_type: str
    id_source: str
    """The HL7 field supplying the resource id. A resource whose id came from the message is
    idempotent on replay; a server-assigned id duplicates the resource every retransmission."""
    id_prefix: str = ""
    fields: tuple[FieldMapping, ...] = ()
    per_segment: str = ""
    """When set, one resource is produced per occurrence of this segment — an ORU with four
    OBX segments produces four Observations."""
    constants: dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass(frozen=True, slots=True)
class MessageMapping:
    """The resources one message family produces."""

    message_type: str
    triggers: frozenset[str]
    resources: tuple[ResourceMapping, ...]

    def handles(self, message_type: str, trigger: str) -> bool:
        if message_type.upper() != self.message_type.upper():
            return False
        return not self.triggers or trigger.upper() in self.triggers


@dataclass(frozen=True, slots=True)
class MappingSet:
    """A versioned set of mappings for one interface."""

    name: str
    version: str
    messages: tuple[MessageMapping, ...]
    source_versions: frozenset[str] = frozenset()
    description: str = ""

    def for_message(self, message_type: str, trigger: str) -> MessageMapping | None:
        for mapping in self.messages:
            if mapping.handles(message_type, trigger):
                return mapping
        return None

    def consumed_fields(self) -> tuple[str, ...]:
        """Every HL7 field this set reads.

        The other half of coverage: an operator can compare this against what a sender actually
        transmits and see what arrives that nothing looks at.
        """
        found: set[str] = set()
        for message in self.messages:
            for resource in message.resources:
                found.add(resource.id_source)
                found.update(f.source for f in resource.fields)
        return tuple(sorted(found))

    def key(self) -> str:
        return f"{self.name}@{self.version}"


def _validate_target(resource_type: str, target: str) -> None:
    """Refuse a target path whose first step is not a real element."""
    definition = definition_for(resource_type)
    if definition is None:
        raise MappingError(
            f"mapping targets resource type {resource_type!r}, which this gateway does not serve"
        )
    first = target.split(".")[0]
    match = _TARGET_STEP.match(first)
    if not match:
        raise MappingError(f"target path {target!r} is malformed")
    name = match.group("name")
    if not any(e.name == name for e in definition.elements):
        raise MappingError(
            f"{resource_type}.{name} is not an element of {resource_type}. A mapping to a "
            "non-existent element writes a field no validator checks and no consumer reads."
        )


def _parse_field(raw: dict[str, Any], resource_type: str) -> FieldMapping:
    for required_key in ("source", "target"):
        if required_key not in raw:
            raise MappingError(f"field mapping is missing {required_key!r}: {raw}")
    unknown = set(raw) - {"source", "target", "transform", "params", "required", "note"}
    if unknown:
        raise MappingError(
            f"field mapping has unknown keys {sorted(unknown)}; a misspelled key is silently "
            "ignored, so it is refused instead"
        )
    source = str(raw["source"])
    if not _SOURCE.match(source):
        raise MappingError(
            f"source {source!r} is not an HL7 path (SEG-field[.component[.subcomponent]][[*]])"
        )
    transform = str(raw.get("transform", "identity"))
    if transform not in TRANSFORMS:
        raise MappingError(
            f"unknown transform {transform!r}; available: {', '.join(transform_names())}"
        )
    target = str(raw["target"])
    _validate_target(resource_type, target)
    return FieldMapping(
        source=source,
        target=target,
        transform=transform,
        params=dict(raw.get("params") or {}),
        required=bool(raw.get("required", False)),
        note=str(raw.get("note", "")),
    )


def _parse_resource(raw: dict[str, Any]) -> ResourceMapping:
    resource_type = str(raw.get("resource", ""))
    if not resource_type:
        raise MappingError("resource mapping has no 'resource' key")
    if "id_source" not in raw:
        raise MappingError(
            f"{resource_type} mapping has no id_source. A resource whose id is assigned by the "
            "server duplicates on every retransmission of the same message."
        )
    fields = tuple(_parse_field(f, resource_type) for f in raw.get("fields") or [])

    seen: dict[str, str] = {}
    for mapping in fields:
        if mapping.target in seen:
            raise MappingError(
                f"{resource_type}.{mapping.target} is written by both {seen[mapping.target]!r} "
                f"and {mapping.source!r}; which wins would depend on file order"
            )
        seen[mapping.target] = mapping.source

    return ResourceMapping(
        resource_type=resource_type,
        id_source=str(raw["id_source"]),
        id_prefix=str(raw.get("id_prefix", "")),
        fields=fields,
        per_segment=str(raw.get("per_segment", "")),
        constants=dict(raw.get("constants") or {}),
        note=str(raw.get("note", "")),
    )


def load_mapping_set(path: pathlib.Path | str) -> MappingSet:
    """Load and validate a mapping file."""
    location = pathlib.Path(path)
    try:
        raw = yaml.safe_load(location.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MappingError(f"{location.name} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise MappingError(f"{location.name} must contain a mapping at the top level")

    for required_key in ("name", "version", "messages"):
        if required_key not in raw:
            raise MappingError(
                f"{location.name} has no {required_key!r}. A mapping set without a version "
                "cannot be rolled back or investigated after an incident."
            )

    messages = []
    for entry in raw["messages"]:
        if "message_type" not in entry:
            raise MappingError(f"{location.name}: message mapping has no message_type")
        messages.append(
            MessageMapping(
                message_type=str(entry["message_type"]),
                triggers=frozenset(str(t).upper() for t in entry.get("triggers") or []),
                resources=tuple(_parse_resource(r) for r in entry.get("produces") or []),
            )
        )

    mapping_set = MappingSet(
        name=str(raw["name"]),
        version=str(raw["version"]),
        messages=tuple(messages),
        source_versions=frozenset(str(v) for v in raw.get("source_versions") or []),
        description=str(raw.get("description", "")),
    )
    _log.info(
        "mapping.loaded",
        mapping_set=mapping_set.key(),
        messages=len(mapping_set.messages),
        fields=len(mapping_set.consumed_fields()),
    )
    return mapping_set


def _read_source(message: Hl7Message, source: str, *, segment_index: int = 0) -> list[str]:
    """Read an HL7 path, returning every repetition."""
    match = _SOURCE.match(source)
    if not match:
        raise MappingError(f"source {source!r} is not an HL7 path")
    name = match.group("segment")
    number = int(match.group("field"))
    component = int(match.group("component") or 1)
    subcomponent = int(match.group("subcomponent") or 1)
    wants_all = bool(match.group("repeat"))

    segments = message.named(name)
    if not segments:
        return []
    segment = segments[segment_index] if segment_index < len(segments) else segments[0]

    if wants_all:
        return [
            segment.get(number, repeat=i, component=component, subcomponent=subcomponent)
            for i in range(max(1, segment.repeat_count(number)))
        ]
    return [segment.get(number, component=component, subcomponent=subcomponent)]


def _set_path(target: dict[str, Any], path: str, value: Any, *, repeat_index: int = 0) -> None:
    """Write a value at a dotted path, creating lists and objects as needed.

    ``[*]`` in the path means "the repeat index of the source", which is what makes a repeating
    ``PID-3`` produce several identifier entries rather than overwriting one.
    """
    steps = path.split(".")
    current: Any = target
    for position, step in enumerate(steps):
        match = _TARGET_STEP.match(step)
        if not match:
            raise MappingError(f"malformed target path step {step!r} in {path!r}")
        name = match.group("name")
        index_token = match.group("index")
        last = position == len(steps) - 1

        if index_token is None:
            if last:
                current[name] = value
                return
            current = current.setdefault(name, {})
            continue

        index = repeat_index if index_token == "*" else int(index_token)
        container = current.setdefault(name, [])
        if not isinstance(container, list):
            raise MappingError(f"target {path!r} indexes {name!r}, which is not a list")
        while len(container) <= index:
            container.append({})
        if last:
            container[index] = value
            return
        current = container[index]


@dataclass(frozen=True, slots=True)
class MappingResult:
    """What one message produced."""

    resources: tuple[Resource, ...] = ()
    mapping_set: str = ""
    unmapped_segments: tuple[str, ...] = ()
    """Segments present in the message that no mapping reads. Reported, because a field nobody
    mapped is absent and absent is easy to miss."""
    warnings: tuple[str, ...] = ()

    @property
    def produced(self) -> int:
        return len(self.resources)


class MappingEngine:
    """Applies a mapping set to messages."""

    def __init__(self, mapping_set: MappingSet) -> None:
        self._set = mapping_set

    @property
    def mapping_set(self) -> MappingSet:
        return self._set

    def apply(self, message: Hl7Message, *, organization_id: str) -> MappingResult:
        """Turn one message into resources.

        Raises :class:`MappingError` when no mapping handles the message. Producing nothing
        quietly would let an unmapped message type be acknowledged and dropped.
        """
        mapping = self._set.for_message(message.message_type, message.trigger_event)
        if mapping is None:
            raise MappingError(
                f"mapping set {self._set.key()} does not handle "
                f"{message.message_type}^{message.trigger_event}"
            )

        resources: list[Resource] = []
        warnings: list[str] = []
        read_segments: set[str] = {"MSH"}

        for resource_mapping in mapping.resources:
            read_segments.update(f.segment for f in resource_mapping.fields)
            read_segments.add(_SOURCE.match(resource_mapping.id_source).group("segment"))  # type: ignore[union-attr]

            occurrences = (
                len(message.named(resource_mapping.per_segment))
                if resource_mapping.per_segment
                else 1
            )
            if resource_mapping.per_segment and occurrences == 0:
                continue

            for index in range(occurrences):
                built, issues = self._build(
                    resource_mapping, message, index, organization_id=organization_id
                )
                warnings.extend(issues)
                if built is not None:
                    resources.append(built)

        present = set(message.segment_names())
        unmapped = tuple(sorted(present - read_segments))

        return MappingResult(
            resources=tuple(resources),
            mapping_set=self._set.key(),
            unmapped_segments=unmapped,
            warnings=tuple(warnings),
        )

    def _build(
        self,
        mapping: ResourceMapping,
        message: Hl7Message,
        segment_index: int,
        *,
        organization_id: str,
    ) -> tuple[Resource | None, list[str]]:
        warnings: list[str] = []
        data: dict[str, Any] = {"resourceType": mapping.resource_type}
        data.update(mapping.constants)

        raw_ids = _read_source(message, mapping.id_source, segment_index=segment_index)
        raw_id = next((value for value in raw_ids if value), "")
        if not raw_id:
            return None, [
                f"{mapping.resource_type} skipped: id source {mapping.id_source} is empty, and "
                "a resource with no stable id duplicates on retransmission"
            ]
        data["id"] = _sanitise_id(f"{mapping.id_prefix}{raw_id}")

        for field_mapping in mapping.fields:
            values = _read_source(message, field_mapping.source, segment_index=segment_index)
            if not field_mapping.repeats:
                values = values[:1]
            wrote = False
            for repeat_index, raw_value in enumerate(values):
                if not raw_value:
                    continue
                transform = TRANSFORMS[field_mapping.transform]
                try:
                    converted = transform.apply(raw_value, field_mapping.params)
                except MappingError as exc:
                    warnings.append(f"{field_mapping.source} -> {field_mapping.target}: {exc}")
                    continue
                if converted is None:
                    continue
                _set_path(data, field_mapping.target, converted, repeat_index=repeat_index)
                wrote = True
            if field_mapping.required and not wrote:
                return None, [
                    *warnings,
                    f"{mapping.resource_type} skipped: required mapping "
                    f"{field_mapping.source} -> {field_mapping.target} produced nothing",
                ]

        return Resource(
            resource_type=mapping.resource_type,
            data=data,
            organization_id=organization_id,
        ), warnings


def _sanitise_id(value: str) -> str:
    """Make an HL7 identifier safe as a FHIR id.

    FHIR ids allow ``A-Z a-z 0-9 - .`` up to 64 characters. Substituting rather than rejecting,
    because an identifier containing a space or a slash is common and is not a reason to drop a
    clinical message — but the substitution is deterministic, so the same source identifier
    always produces the same FHIR id and retransmission stays idempotent.
    """
    cleaned = re.sub(r"[^A-Za-z0-9\-.]", "-", value.strip())
    return cleaned[:64] or "unknown"
