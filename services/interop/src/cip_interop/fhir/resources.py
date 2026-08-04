"""FHIR resource instances.

A resource is a JSON object plus its type. Deliberately not a class hierarchy: FHIR resources
are open (extensions can appear anywhere), evolving (R4 and R5 differ), and mostly passed
through rather than manipulated field by field. A class per resource would need regenerating
for every version and would silently drop anything it did not model — and silently dropping an
extension is how a site's only record of something disappears in transit.

Structure is enforced by validation against the declared definitions
(:mod:`cip_interop.fhir.definitions`), which is where FHIR itself puts it.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field, replace
from typing import Any

from cip_interop.domain import ValidationError
from cip_interop.fhir.definitions import (
    RETRACTED_STATUSES,
    FhirVersion,
    ResourceDefinition,
    definition_for,
)

__all__ = [
    "Reference",
    "Resource",
    "fhir_id",
    "make_reference",
    "parse_reference",
]

_REFERENCE = re.compile(
    r"^(?:(?P<base>https?://[^\s]+?)/)?(?P<type>[A-Z][A-Za-z]+)/(?P<id>[A-Za-z0-9\-.]{1,64})(?:/_history/(?P<version>[A-Za-z0-9\-.]{1,64}))?$"
)


@dataclass(frozen=True, slots=True)
class Reference:
    """A parsed literal reference.

    ``resource_type`` is mandatory. FHIR allows a bare relative reference without one, and
    every implementation that accepts it ends up guessing the type from context — which is how
    an Observation ends up subject-referencing a Practitioner.
    """

    resource_type: str
    resource_id: str
    version_id: str = ""
    base_url: str = ""

    def render(self) -> str:
        head = f"{self.base_url}/" if self.base_url else ""
        tail = f"/_history/{self.version_id}" if self.version_id else ""
        return f"{head}{self.resource_type}/{self.resource_id}{tail}"

    @property
    def is_versioned(self) -> bool:
        return bool(self.version_id)


def parse_reference(value: str) -> Reference | None:
    """Parse a literal reference string, or ``None`` if it is not one.

    ``None`` rather than an exception: a reference element may legitimately carry only a
    ``display`` or an ``identifier`` (a logical reference), and those are not errors.
    """
    match = _REFERENCE.match(value.strip())
    if not match:
        return None
    return Reference(
        resource_type=match.group("type"),
        resource_id=match.group("id"),
        version_id=match.group("version") or "",
        base_url=match.group("base") or "",
    )


def fhir_id(value: str) -> str:
    """Map an internal identifier onto the FHIR id character set.

    FHIR permits ``A-Z a-z 0-9 - .`` up to 64 characters. Internal identifiers legitimately
    look like ``org:mercy-general``, and a colon makes every reference built from one invalid —
    a failure that surfaces far from its cause, in whatever consumer dereferences it.

    Deterministic, so the same internal id always yields the same FHIR id and references stay
    resolvable across processes and restarts.
    """
    cleaned = re.sub(r"[^A-Za-z0-9\-.]", "-", value.strip())
    return cleaned[:64] or "unknown"


def make_reference(resource_type: str, resource_id: str, *, display: str = "") -> dict[str, Any]:
    reference: dict[str, Any] = {"reference": f"{resource_type}/{fhir_id(resource_id)}"}
    if display:
        reference["display"] = display
    return reference


@dataclass(frozen=True, slots=True)
class Resource:
    """One FHIR resource.

    Immutable. Updates produce a new instance, which is what makes the version history in the
    repository a history rather than a log of mutations to one object.
    """

    resource_type: str
    data: dict[str, Any] = field(default_factory=dict)
    version_id: str = ""
    last_updated: dt.datetime | None = None
    #: The organisation that holds this resource. Carried on the resource rather than derived
    #: at query time, because a resource whose owner is only known by where it was found is one
    #: that loses its owner the moment it is copied. See
    #: docs/design/adr-0030-cross-organisation-sharing.md.
    organization_id: str = ""

    def __post_init__(self) -> None:
        if not self.resource_type.strip():
            raise ValidationError("Resource.resource_type must not be empty")
        declared = self.data.get("resourceType")
        if declared is not None and declared != self.resource_type:
            raise ValidationError(
                f"resourceType in the payload is {declared!r} but the resource is typed "
                f"{self.resource_type!r}; one of the two is wrong and guessing which would "
                "store the resource under a type nothing searches"
            )

    @property
    def id(self) -> str:
        value = self.data.get("id", "")
        return value if isinstance(value, str) else ""

    @property
    def definition(self) -> ResourceDefinition | None:
        return definition_for(self.resource_type)

    @property
    def status(self) -> str:
        value = self.data.get("status", "")
        return value if isinstance(value, str) else ""

    @property
    def is_retracted(self) -> bool:
        """Whether this resource no longer asserts what it says.

        ``entered-in-error`` is the one that matters clinically: a lab result the lab has
        retracted must not be returned as current, and a search that ignores status will hand a
        clinician a value that has been withdrawn.
        """
        return self.status in RETRACTED_STATUSES

    def reference(self) -> str:
        return f"{self.resource_type}/{self.id}"

    def get(self, path: str, default: Any = None) -> Any:
        """Read a dotted path.

        List elements are traversed by index (``name.0.family``). Returns ``default`` for any
        missing step rather than raising, because a missing optional element is the normal case
        and a validator has already established what must be present.
        """
        current: Any = self.data
        for part in path.split("."):
            if isinstance(current, dict):
                if part not in current:
                    return default
                current = current[part]
            elif isinstance(current, list):
                if not part.isdigit() or int(part) >= len(current):
                    return default
                current = current[int(part)]
            else:
                return default
        return current

    def patient_reference(self) -> str:
        """The patient this resource is about, as a literal reference.

        Read from the declared ``patient_element`` rather than by guessing between ``subject``
        and ``patient``. Both exist in FHIR on different resources, and a guess is wrong on
        whichever half of them it did not guess.
        """
        definition = self.definition
        if definition is None or not definition.patient_element:
            return ""
        value = self.get(f"{definition.patient_element}.reference", "")
        return value if isinstance(value, str) else ""

    def with_data(self, data: dict[str, Any]) -> Resource:
        return replace(self, data=data)

    def with_version(self, version_id: str, *, at: dt.datetime | None = None) -> Resource:
        payload = dict(self.data)
        meta = dict(payload.get("meta") or {})
        meta["versionId"] = version_id
        moment = at or dt.datetime.now(dt.UTC)
        meta["lastUpdated"] = moment.isoformat()
        payload["meta"] = meta
        return replace(self, data=payload, version_id=version_id, last_updated=moment)

    def to_json(self) -> dict[str, Any]:
        payload = dict(self.data)
        payload["resourceType"] = self.resource_type
        return payload

    def summary(self) -> dict[str, Any]:
        """The ``_summary=true`` projection.

        Only elements marked as summary in the definition survive. Useful because a summary is
        often shareable where the full resource is not — and building it from the definition
        rather than by hand means it cannot drift into leaking a non-summary element.
        """
        definition = self.definition
        if definition is None:
            return {"resourceType": self.resource_type, "id": self.id}
        keep = {e.name for e in definition.elements if e.summary}
        payload = {k: v for k, v in self.data.items() if k in keep or k in ("resourceType", "id")}
        payload["resourceType"] = self.resource_type
        payload["meta"] = {"tag": [{"code": "SUBSETTED"}]}
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any], *, organization_id: str = "") -> Resource:
        resource_type = payload.get("resourceType")
        if not isinstance(resource_type, str) or not resource_type:
            raise ValidationError(
                "payload has no resourceType; a FHIR resource without one cannot be validated, "
                "routed, or stored"
            )
        meta = payload.get("meta") or {}
        version_id = meta.get("versionId", "") if isinstance(meta, dict) else ""
        return cls(
            resource_type=resource_type,
            data=payload,
            version_id=version_id if isinstance(version_id, str) else "",
            organization_id=organization_id,
        )


def version_of(payload: dict[str, Any]) -> FhirVersion | None:
    """The FHIR version a payload claims, from ``meta.profile`` or an explicit marker.

    Returns ``None`` when the payload does not say — which is the common case, and the caller
    must then use the negotiated version rather than assuming one.
    """
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None
    for profile in meta.get("profile", []) or []:
        if isinstance(profile, str):
            if "/4.0" in profile:
                return FhirVersion.R4
            if "/5.0" in profile:
                return FhirVersion.R5
    return None
